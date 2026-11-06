// Refresh Switch: sets the main monitor's refresh rate when the game starts and puts the old rate back when it ends, as a plain SKSE / F4SE plugin. One DLL for
// Skyrim SE (1.5.97 to 1.7.x) and Fallout 4 (1.10.163 to 1.11.x): it reads no game memory, so it has no game version to match. Plain C++ and the Windows SDK.
//
// What it does: at the loader's "all plugins are loaded" message, ahead of the game's window, it reads fRefreshRate from RefreshSwitch.ini beside the DLL. It does
// nothing when the rate is unset, when REFRESH_SWITCH_OFF or HDR_SWITCH_OFF is in the environment (test runners set one), when the game is not on the normal desktop
// (a hidden test game), when the monitor does not offer the rate, or when the monitor is already at it. Otherwise it starts a small watcher, then changes the rate.
// The watcher is rundll32 running this same DLL's RefreshSwitchWatch, which waits for the game process to end, however it ends, and puts the old rate back.
//
// The change is never saved to Windows' settings, so a reboot or a power cut brings the old rate back by itself.
//
// Taking turns with HDR Switch, without needing it or changing it: HDR Switch finishes its work while plugins load, so by the "all plugins are loaded" message HDR is
// settled, whatever the load order. At the end both watchers see the game go together; this one waits, for a limited time, until HDR Switch's has finished.
//
// Log: Documents\My Games\<game>\<SKSE or F4SE>\RefreshSwitch.log, started over each launch.
#include <windows.h>

#include <shlobj.h>
#include <tlhelp32.h>

#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cwchar>
#include <string>
#include <vector>

#include "version.h"
#include "xse.h"

namespace
{
	constexpr char    kName[] = "RefreshSwitch";
	constexpr wchar_t kWatchEntry[] = L"RefreshSwitchWatch";
	constexpr wchar_t kHdrWatchEntry[] = L"HdrSwitchWatch";
	constexpr DWORD   kSettleMs = 5000;        // how long to look for the new rate after asking for it
	constexpr DWORD   kSpawnMs = 10000;        // how long the game waits for the helper that starts the watcher
	constexpr DWORD   kHdrTurnMs = 6000;       // how long the watcher waits for HDR Switch's (which itself gives up after 5 s), then restores anyway
	constexpr DWORD   kPreviousGameMs = 12000; // at start, how long to wait for the watcher of a game that just closed
	constexpr DWORD   kReleaseMs = 3000;       // after the game ends, how long the monitor may take to leave a rate exclusive fullscreen had picked
	constexpr DWORD   kWatchStartMs = 60000;   // how long the watcher looks at the rate while the game starts
	constexpr DWORD   kWatchStepMs = 2000;
	constexpr double  kHighestRate = 2000.0;

	struct Rate
	{
		std::uint32_t numerator{ 0 };
		std::uint32_t denominator{ 0 };

		[[nodiscard]] double value() const { return denominator ? static_cast<double>(numerator) / denominator : 0.0; }
		[[nodiscard]] bool   same_as(const Rate& a_other) const { return std::fabs(value() - a_other.value()) < 0.005; }
	};

	struct Monitor
	{
		LUID          adapter{};
		std::uint32_t target{ 0 };
	};

	struct DisplayNow
	{
		Monitor monitor;
		Rate    rate;                 // what the monitor really runs at, 59.94 included
		DWORD   whole{ 0 };           // the same rate as Windows' list of modes writes it: 59 for 59.94
		DWORD   width{ 0 };
		DWORD   height{ 0 };
		wchar_t device[32]{};         // \\.\DISPLAY1
		bool    hdrKnown{ false };
		bool    hdrOn{ false };
	};

	wchar_t g_log[MAX_PATH * 2] = {};
	const char* g_loader = "";
	
	void log_line(const char* a_format, ...)
	{
		if (!g_log[0]) {
			return;
		}
		FILE* file = nullptr;
		if (_wfopen_s(&file, g_log, L"a") != 0 || !file) {
			return;
		}
		SYSTEMTIME now;
		GetLocalTime(&now);
		std::fprintf(file, "[%02u:%02u:%02u.%03u] ", now.wHour, now.wMinute, now.wSecond, now.wMilliseconds);
		va_list args;
		va_start(args, a_format);
		std::vfprintf(file, a_format, args);
		va_end(args);
		std::fputc('\n', file);
		std::fclose(file);
	}

	// Documents\My Games\<game>\<loader>\RefreshSwitch.log. a_fresh: start the file over (a new game); the watcher adds to it.
	void open_log(const char* a_loader, bool a_fresh)
	{
		PWSTR documents = nullptr;
		if (FAILED(SHGetKnownFolderPath(FOLDERID_Documents, 0, nullptr, &documents)) || !documents) {
			return;
		}
		const bool   skse = std::strcmp(a_loader, "SKSE") == 0;
		std::wstring path(documents);
		CoTaskMemFree(documents);
		path += L"\\My Games";
		CreateDirectoryW(path.c_str(), nullptr);
		path += skse ? L"\\Skyrim Special Edition" : L"\\Fallout4";
		CreateDirectoryW(path.c_str(), nullptr);
		path += skse ? L"\\SKSE" : L"\\F4SE";
		CreateDirectoryW(path.c_str(), nullptr);
		path += L"\\RefreshSwitch.log";
		if (path.size() >= std::size(g_log)) {
			return;
		}
		wcscpy_s(g_log, path.c_str());
		if (a_fresh) {
			FILE* file = nullptr;
			if (_wfopen_s(&file, g_log, L"w") == 0 && file) {
				std::fclose(file);
			}
		}
	}

	// Mode lists and sizes must be the real ones, not ones Windows scales for a program that ignores display scaling, as the game and rundll32 both do.
	void use_real_pixels()
	{
		using Set = DPI_AWARENESS_CONTEXT(WINAPI*)(DPI_AWARENESS_CONTEXT);
		const auto user32 = GetModuleHandleW(L"user32.dll");
		const auto set = user32 ? reinterpret_cast<Set>(reinterpret_cast<void*>(GetProcAddress(user32, "SetThreadDpiAwarenessContext"))) : nullptr;
		if (set) {
			set(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
		}
	}

	bool read_config(std::vector<DISPLAYCONFIG_PATH_INFO>& a_paths, std::vector<DISPLAYCONFIG_MODE_INFO>& a_modes)
	{
		LONG result = ERROR_INSUFFICIENT_BUFFER;
		for (int tries = 0; tries < 5 && result == ERROR_INSUFFICIENT_BUFFER; ++tries) {
			UINT32 pathCount = 0;
			UINT32 modeCount = 0;
			if (GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &pathCount, &modeCount) != ERROR_SUCCESS) {
				return false;
			}
			a_paths.resize(pathCount);
			a_modes.resize(modeCount);
			result = QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &pathCount, a_paths.data(), &modeCount, a_modes.data(), nullptr);
			a_paths.resize(pathCount);
			a_modes.resize(modeCount);
		}
		return result == ERROR_SUCCESS;
	}

	[[nodiscard]] bool is_monitor(const DISPLAYCONFIG_PATH_INFO& a_path, const Monitor& a_monitor)
	{
		return a_path.targetInfo.adapterId.LowPart == a_monitor.adapter.LowPart && a_path.targetInfo.adapterId.HighPart == a_monitor.adapter.HighPart &&
		       a_path.targetInfo.id == a_monitor.target;
	}

	// The primary monitor: the active display whose desktop starts at 0,0.
	bool primary_monitor(Monitor& a_out)
	{
		std::vector<DISPLAYCONFIG_PATH_INFO> paths;
		std::vector<DISPLAYCONFIG_MODE_INFO> modes;
		if (!read_config(paths, modes)) {
			return false;
		}
		for (const auto& path : paths) {
			const auto index = path.sourceInfo.modeInfoIdx;
			if (index == DISPLAYCONFIG_PATH_MODE_IDX_INVALID || index >= modes.size() || modes[index].infoType != DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE) {
				continue;
			}
			const auto& position = modes[index].sourceMode.position;
			if (position.x == 0 && position.y == 0) {
				a_out.adapter = path.targetInfo.adapterId;
				a_out.target = path.targetInfo.id;
				return true;
			}
		}
		return false;
	}

	bool read_display(const Monitor& a_monitor, DisplayNow& a_out)
	{
		std::vector<DISPLAYCONFIG_PATH_INFO> paths;
		std::vector<DISPLAYCONFIG_MODE_INFO> modes;
		if (!read_config(paths, modes)) {
			return false;
		}
		for (const auto& path : paths) {
			if (!is_monitor(path, a_monitor)) {
				continue;
			}
			a_out.monitor = a_monitor;
			a_out.rate = { path.targetInfo.refreshRate.Numerator, path.targetInfo.refreshRate.Denominator };
			const auto index = path.targetInfo.modeInfoIdx;
			if (index != DISPLAYCONFIG_PATH_MODE_IDX_INVALID && index < modes.size() && modes[index].infoType == DISPLAYCONFIG_MODE_INFO_TYPE_TARGET) {
				const auto& sync = modes[index].targetMode.targetVideoSignalInfo.vSyncFreq;
				if (sync.Denominator != 0) {
					a_out.rate = { sync.Numerator, sync.Denominator };
				}
			}

			DISPLAYCONFIG_SOURCE_DEVICE_NAME name{};
			name.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
			name.header.size = sizeof(name);
			name.header.adapterId = path.sourceInfo.adapterId;
			name.header.id = path.sourceInfo.id;
			if (DisplayConfigGetDeviceInfo(&name.header) != ERROR_SUCCESS) {
				return false;
			}
			wcscpy_s(a_out.device, name.viewGdiDeviceName);

			DEVMODEW current{};
			current.dmSize = sizeof(current);
			if (!EnumDisplaySettingsExW(a_out.device, ENUM_CURRENT_SETTINGS, &current, 0)) {
				return false;
			}
			a_out.whole = current.dmDisplayFrequency;
			a_out.width = current.dmPelsWidth;
			a_out.height = current.dmPelsHeight;

			// For the log. Windows 11 24H2's own HDR call first: it tells HDR from Auto Color Management, where the older state also reads on.
			struct
			{
				DISPLAYCONFIG_DEVICE_INFO_HEADER header;
				std::uint32_t                    value;
				std::uint32_t                    colorEncoding;
				std::uint32_t                    bitsPerColorChannel;
				std::uint32_t                    activeColorMode;
			} modern{};
			modern.header.type = static_cast<DISPLAYCONFIG_DEVICE_INFO_TYPE>(15);
			modern.header.size = sizeof(modern);
			modern.header.adapterId = path.targetInfo.adapterId;
			modern.header.id = path.targetInfo.id;
			if (DisplayConfigGetDeviceInfo(&modern.header) == ERROR_SUCCESS) {
				a_out.hdrKnown = true;
				a_out.hdrOn = (modern.value & (1u << 5)) != 0;
				return true;
			}
			DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO color{};
			color.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO;
			color.header.size = sizeof(color);
			color.header.adapterId = path.targetInfo.adapterId;
			color.header.id = path.targetInfo.id;
			if (DisplayConfigGetDeviceInfo(&color.header) == ERROR_SUCCESS) {
				a_out.hdrKnown = true;
				a_out.hdrOn = color.advancedColorEnabled != 0;
			}
			return true;
		}
		return false;
	}

	// The rates Windows offers at the monitor's current size, written as its list writes them.
	std::vector<DWORD> offered_rates(const DisplayNow& a_now)
	{
		std::vector<DWORD> rates;
		DEVMODEW           mode{};
		mode.dmSize = sizeof(mode);
		for (DWORD index = 0; EnumDisplaySettingsExW(a_now.device, index, &mode, 0); ++index) {
			const bool interlaced = (mode.dmDisplayFlags & DM_INTERLACED) != 0;
			if (mode.dmPelsWidth == a_now.width && mode.dmPelsHeight == a_now.height && !interlaced && mode.dmDisplayFrequency > 1) {
				rates.push_back(mode.dmDisplayFrequency);
			}
		}
		std::sort(rates.begin(), rates.end());
		rates.erase(std::unique(rates.begin(), rates.end()), rates.end());
		return rates;
	}

	// Which listed rate a request means. Windows lists 59.94 as 59, and some monitors list 119.88 as 119 or 143.98 as 143, so 59.94 means 59, and 120 means 120 or,
	// when only that is offered, 119. 0 when the monitor offers neither.
	DWORD listed_rate(double a_wanted, const std::vector<DWORD>& a_offered)
	{
		const auto has = [&](DWORD a_rate) { return std::find(a_offered.begin(), a_offered.end(), a_rate) != a_offered.end(); };
		const auto rounded = static_cast<DWORD>(std::lround(a_wanted));
		const auto below = static_cast<DWORD>(std::floor(a_wanted));
		const bool whole = std::fabs(a_wanted - rounded) < 0.005;
		if (whole) {
			return has(rounded) ? rounded : (has(rounded - 1) ? rounded - 1 : 0);
		}
		return has(below) ? below : (has(rounded) ? rounded : 0);
	}

	std::string list_text(const std::vector<DWORD>& a_rates)
	{
		std::string text;
		for (const auto rate : a_rates) {
			if (!text.empty()) {
				text += ", ";
			}
			text += std::to_string(rate);
		}
		return text.empty() ? "none" : text;
	}

	// Change the rate only, for now only: without CDS_UPDATEREGISTRY nothing is saved to Windows' settings. a_flags: 0 to change it, CDS_TEST to ask whether it would work.
	LONG change_to_listed(const wchar_t* a_device, DWORD a_rate, DWORD a_flags = 0)
	{
		DEVMODEW mode{};
		mode.dmSize = sizeof(mode);
		mode.dmFields = DM_DISPLAYFREQUENCY;
		mode.dmDisplayFrequency = a_rate;
		return ChangeDisplaySettingsExW(a_device, &mode, nullptr, a_flags, nullptr);
	}

	// Put back an exact rate such as 59.94, which the list's whole numbers cannot name. Also not saved to Windows' settings.
	LONG change_to_exact(const Monitor& a_monitor, const Rate& a_rate)
	{
		std::vector<DISPLAYCONFIG_PATH_INFO> paths;
		std::vector<DISPLAYCONFIG_MODE_INFO> modes;
		if (!read_config(paths, modes)) {
			return ERROR_GEN_FAILURE;
		}
		for (auto& path : paths) {
			if (is_monitor(path, a_monitor)) {
				path.targetInfo.modeInfoIdx = DISPLAYCONFIG_PATH_MODE_IDX_INVALID;
				path.targetInfo.refreshRate.Numerator = a_rate.numerator;
				path.targetInfo.refreshRate.Denominator = a_rate.denominator;
				return SetDisplayConfig(static_cast<UINT32>(paths.size()), paths.data(), static_cast<UINT32>(modes.size()), modes.data(),
					SDC_APPLY | SDC_USE_SUPPLIED_DISPLAY_CONFIG);
			}
		}
		return ERROR_NOT_FOUND;
	}

	template <class Done>
	bool wait_until(Done a_done, DWORD a_limit)
	{
		const auto started = GetTickCount64();
		for (;;) {
			if (a_done()) {
				return true;
			}
			if (GetTickCount64() - started >= a_limit) {
				return false;
			}
			Sleep(50);
		}
	}

	bool on_normal_desktop()
	{
		wchar_t name[256] = {};
		DWORD   needed = 0;
		HDESK   desktop = GetThreadDesktop(GetCurrentThreadId());
		if (!desktop || !GetUserObjectInformationW(desktop, UOI_NAME, name, sizeof(name) - sizeof(wchar_t), &needed)) {
			return true;
		}
		return _wcsicmp(name, L"Default") == 0;
	}

	std::wstring module_path()
	{
		HMODULE self = nullptr;
		GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT, reinterpret_cast<LPCWSTR>(&module_path), &self);
		wchar_t path[MAX_PATH * 2] = {};
		GetModuleFileNameW(self, path, static_cast<DWORD>(std::size(path)));
		return path;
	}

	// fRefreshRate in [General] of RefreshSwitch.ini beside the DLL. 0 when there is no file, it is empty or it is not a usable number.
	double rate_in_ini(const std::wstring& a_dll)
	{
		const auto slash = a_dll.find_last_of(L"\\/");
		const auto ini = (slash == std::wstring::npos ? std::wstring() : a_dll.substr(0, slash + 1)) + L"RefreshSwitch.ini";
		wchar_t    text[32] = {};
		GetPrivateProfileStringW(L"General", L"fRefreshRate", L"", text, static_cast<DWORD>(std::size(text)), ini.c_str());
		for (auto& letter : text) {
			if (letter == L',') {
				letter = L'.';
			}
		}
		wchar_t*     end = nullptr;
		const double value = std::wcstod(text, &end);
		log_line("ini: fRefreshRate=%ls", text);
		if (end == text) {
			return 0.0;
		}
		if (value < 0.0 || value > kHighestRate) {
			log_line("ini: %ls is not a refresh rate", text);
			return 0.0;
		}
		return value;
	}

	// rundll32 "<this DLL>",<a_entry> <a_arguments>
	bool run_helper(const std::wstring& a_dll, const wchar_t* a_entry, const std::wstring& a_arguments, PROCESS_INFORMATION& a_process)
	{
		wchar_t system[MAX_PATH] = {};
		if (!GetSystemDirectoryW(system, MAX_PATH)) {
			return false;
		}
		const std::wstring exe = std::wstring(system) + L"\\rundll32.exe";
		std::wstring       command = L"\"" + exe + L"\" \"" + a_dll + L"\"," + a_entry + L" " + a_arguments;
		STARTUPINFOW       startup{};
		startup.cb = sizeof(startup);
		return CreateProcessW(exe.c_str(), command.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr, nullptr, &startup, &a_process) != FALSE;
	}

	// The game starts a short-lived helper (RefreshSwitchSpawn), which starts the watcher and exits. So the watcher's parent is already gone and it isn't part of the
	// game's process tree: ending the whole tree ends the game alone, and the watcher is still there to restore the rate.
	// Arguments for both: <game pid> <adapter low> <adapter high> <target> <loader> <old rate numerator> <old rate denominator> <old rate as listed> <new rate as listed>
	bool start_watcher(const std::wstring& a_dll, const DisplayNow& a_now, DWORD a_listed)
	{
		wchar_t arguments[160];
		swprintf_s(arguments, L"%lu %lu %ld %u %hs %u %u %lu %lu", GetCurrentProcessId(), a_now.monitor.adapter.LowPart, a_now.monitor.adapter.HighPart, a_now.monitor.target,
			g_loader, a_now.rate.numerator, a_now.rate.denominator, a_now.whole, a_listed);
		PROCESS_INFORMATION process{};
		if (!run_helper(a_dll, L"RefreshSwitchSpawn", arguments, process)) {
			log_line("could not start the watcher: error %lu", GetLastError());
			return false;
		}
		DWORD      code = 1;
		const bool ended = WaitForSingleObject(process.hProcess, kSpawnMs) == WAIT_OBJECT_0;
		if (ended) {
			GetExitCodeProcess(process.hProcess, &code);
		} else {
			TerminateProcess(process.hProcess, 1);
		}
		CloseHandle(process.hThread);
		CloseHandle(process.hProcess);
		if (!ended || code != 0) {
			log_line("could not start the watcher: helper %s, code %lu", ended ? "failed" : "timed out", code);
			return false;
		}
		return true;
	}

	struct Watcher
	{
		DWORD pid{ 0 };
		DWORD gamePid{ 0 };
	};

	// Every running rundll32 with a_entry on its command line, and the game pid after it. Windows gives a process's command line only through this ntdll call.
	std::vector<Watcher> find_watchers(const wchar_t* a_entry)
	{
		std::vector<Watcher> found;
		using Query = LONG(NTAPI*)(HANDLE, ULONG, PVOID, ULONG, PULONG);
		constexpr ULONG kCommandLineInformation = 60;
		const auto      ntdll = GetModuleHandleW(L"ntdll.dll");
		const auto      query = ntdll ? reinterpret_cast<Query>(reinterpret_cast<void*>(GetProcAddress(ntdll, "NtQueryInformationProcess"))) : nullptr;
		if (!query) {
			return found;
		}
		const auto snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
		if (snapshot == INVALID_HANDLE_VALUE) {
			return found;
		}
		PROCESSENTRY32W entry{};
		entry.dwSize = sizeof(entry);
		for (auto more = Process32FirstW(snapshot, &entry); more; more = Process32NextW(snapshot, &entry)) {
			if (_wcsicmp(entry.szExeFile, L"rundll32.exe") != 0) {
				continue;
			}
			const auto process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, entry.th32ProcessID);
			if (!process) {
				continue;
			}
			struct CommandLine
			{
				USHORT Length;
				USHORT MaximumLength;
				PWSTR  Buffer;
			};
			alignas(CommandLine) char buffer[8192] = {};
			ULONG                     size = 0;
			if (query(process, kCommandLineInformation, buffer, sizeof(buffer) - sizeof(wchar_t), &size) >= 0) {
				const auto*  text = reinterpret_cast<const CommandLine*>(buffer);
				std::wstring line(text->Buffer, text->Length / sizeof(wchar_t));
				const auto   at = line.find(a_entry);
				if (at != std::wstring::npos) {
					found.push_back({ entry.th32ProcessID, static_cast<DWORD>(std::wcstoul(line.c_str() + at + std::wcslen(a_entry), nullptr, 10)) });
				}
			}
			CloseHandle(process);
		}
		CloseHandle(snapshot);
		return found;
	}

	// Wait until these watchers have ended, a_limit in all. False when one was still running at the limit.
	bool wait_for(const std::vector<Watcher>& a_watchers, DWORD a_limit)
	{
		const auto started = GetTickCount64();
		bool       allEnded = true;
		for (const auto& watcher : a_watchers) {
			const auto process = OpenProcess(SYNCHRONIZE, FALSE, watcher.pid);
			if (!process) {
				continue;
			}
			const auto spent = GetTickCount64() - started;
			const auto left = spent >= a_limit ? 0 : static_cast<DWORD>(a_limit - spent);
			allEnded = WaitForSingleObject(process, left) == WAIT_OBJECT_0 && allEnded;
			CloseHandle(process);
		}
		return allEnded;
	}

	// A game just closed and its watcher is still putting the rate back: let it finish, or this game would read the rate it is about to lose as the old one.
	void wait_for_closed_game()
	{
		std::vector<Watcher> finishing;
		for (const auto& watcher : find_watchers(kWatchEntry)) {
			const auto game = OpenProcess(SYNCHRONIZE, FALSE, watcher.gamePid);
			const bool gone = !game || WaitForSingleObject(game, 0) == WAIT_OBJECT_0;
			if (game) {
				CloseHandle(game);
			}
			if (gone) {
				finishing.push_back(watcher);
			}
		}
		if (!finishing.empty()) {
			log_line("a game just closed: waiting for its watcher to put the rate back");
			log_line(wait_for(finishing, kPreviousGameMs) ? "its watcher finished" : "its watcher was still running at the limit: going ahead");
		}
	}

	struct WindowCount
	{
		DWORD pid;
		int   all;
		int   visible;
	};

	BOOL CALLBACK count_window(HWND a_window, LPARAM a_count)
	{
		auto* count = reinterpret_cast<WindowCount*>(a_count);
		DWORD owner = 0;
		GetWindowThreadProcessId(a_window, &owner);
		if (owner == count->pid) {
			++count->all;
			count->visible += IsWindowVisible(a_window) ? 1 : 0;
		}
		return TRUE;
	}

	// The message came. Everything the plugin decides, it decides here.
	void act()
	{
		WindowCount windows{ GetCurrentProcessId(), 0, 0 };
		EnumWindows(count_window, reinterpret_cast<LPARAM>(&windows));
		log_line("all plugins loaded; game windows open: %d, %d visible; HDR Switch %s", windows.all, windows.visible, GetModuleHandleW(L"HdrSwitch.dll") ? "loaded" : "not loaded");

		wchar_t testing[8] = {};
		for (const auto* variable : { L"REFRESH_SWITCH_OFF", L"HDR_SWITCH_OFF" }) {
			if (GetEnvironmentVariableW(variable, testing, static_cast<DWORD>(std::size(testing))) > 0) {
				log_line("skipped: %ls is set (a test run)", variable);
				return;
			}
		}
		if (!on_normal_desktop()) {
			log_line("skipped: the game is on a hidden desktop (a test run)");
			return;
		}
		const auto dll = module_path();
		const auto wanted = rate_in_ini(dll);
		if (wanted <= 0.0) {
			log_line("skipped: no rate set in RefreshSwitch.ini");
			return;
		}

		use_real_pixels();
		wait_for_closed_game();
		Monitor    monitor;
		DisplayNow now;
		if (!primary_monitor(monitor) || !read_display(monitor, now)) {
			log_line("skipped: could not read the primary monitor");
			return;
		}
		const auto offered = offered_rates(now);
		log_line("monitor %ls: %lux%lu at %.3f Hz (listed as %lu), HDR %s; offers %s", now.device, now.width, now.height, now.rate.value(), now.whole,
			now.hdrKnown ? (now.hdrOn ? "on" : "off") : "unknown", list_text(offered).c_str());

		const auto listed = listed_rate(wanted, offered);
		if (listed == 0) {
			log_line("skipped: the monitor does not offer %.3f Hz at this size; rate left alone", wanted);
			return;
		}
		if (listed == now.whole) {
			log_line("skipped: already at %.3f Hz; leaving it alone, now and at exit", now.rate.value());
			return;
		}
		const auto test = change_to_listed(now.device, listed, CDS_TEST);
		if (test != DISP_CHANGE_SUCCESSFUL) {
			log_line("skipped: Windows refused %lu Hz (answer %ld); rate left alone", listed, test);
			return;
		}

		// The watcher first: however early the game dies from here on, something is there to put the rate back.
		if (!start_watcher(dll, now, listed)) {
			log_line("rate left alone: nothing would put it back at exit");
			return;
		}
		const auto started = GetTickCount64();
		const auto answer = change_to_listed(now.device, listed);
		if (answer != DISP_CHANGE_SUCCESSFUL) {
			log_line("Windows refused %lu Hz (answer %ld); rate left alone", listed, answer);
			return;
		}
		DisplayNow after;
		const bool changed = wait_until([&] { return read_display(monitor, after) && after.whole == listed; }, kSettleMs);
		log_line(changed ? "rate now %.3f Hz, %llu ms" : "rate did NOT change: now %.3f Hz after %llu ms", after.rate.value(), GetTickCount64() - started);
	}

	void on_message(xse::Message* a_message)
	{
		static bool done = false;
		if (a_message && a_message->type == xse::kPostPostLoad && !done) {
			done = true;
			act();
		}
	}

	bool on_load(const xse::Interface* a_xse, const char* a_loader, std::uint32_t a_messagingId)
	{
		if (!a_xse || a_xse->isEditor) {
			return false;
		}
		static bool done = false;
		if (done) {
			return true;
		}
		done = true;
		g_loader = a_loader;
		open_log(a_loader, true);
		const auto game = a_xse->runtimeVersion;
		log_line("%s %s, %s, game %u.%u.%u", kName, REFRESHSWITCH_VERSION, a_loader, game >> 24, (game >> 16) & 0xFF, (game >> 4) & 0xFFF);
		const auto* messaging = a_xse->QueryInterface ? static_cast<const xse::MessagingInterface*>(a_xse->QueryInterface(a_messagingId)) : nullptr;
		if (!messaging || !a_xse->GetPluginHandle || !messaging->RegisterListener(a_xse->GetPluginHandle(), a_loader, reinterpret_cast<void*>(&on_message))) {
			log_line("skipped: the loader would not send the all-plugins-loaded message; rate left alone");
		}
		return true;
	}

	bool query(const xse::Interface* a_xse, xse::PluginInfo* a_info)
	{
		if (a_info) {
			a_info->infoVersion = 1;
			a_info->name = kName;
			a_info->version = 1;
		}
		return a_xse && !a_xse->isEditor;
	}

	constexpr std::uint32_t kPacked = (static_cast<std::uint32_t>(REFRESHSWITCH_MAJOR & 0xFF) << 24) | (static_cast<std::uint32_t>(REFRESHSWITCH_MINOR & 0xFF) << 16) |
	                                  (static_cast<std::uint32_t>(REFRESHSWITCH_PATCH & 0xFFF) << 4);
}

extern "C"
{
	__declspec(dllexport) xse::SKSEPluginVersionData SKSEPlugin_Version = {
		.dataVersion = xse::SKSEPluginVersionData::kVersion,
		.pluginVersion = kPacked,
		.pluginName = "RefreshSwitch",
		.author = "sayngwyn",
		.supportEmail = "",
		.versionIndependenceEx = xse::SKSEPluginVersionData::kNoStructUse,
		.versionIndependence = xse::SKSEPluginVersionData::kSignatures | xse::SKSEPluginVersionData::kStructsPost629,
		.compatibleVersions = {},
		.xseMinimum = 0,
	};

	__declspec(dllexport) xse::F4SEPluginVersionData F4SEPlugin_Version = {
		.dataVersion = xse::F4SEPluginVersionData::kVersion,
		.pluginVersion = kPacked,
		.pluginName = "RefreshSwitch",
		.author = "sayngwyn",
		.addressIndependence = xse::F4SEPluginVersionData::kSignatures,
		.structureIndependence = xse::F4SEPluginVersionData::kNoStructs,
		.compatibleVersions = {},
		.xseMinimum = 0,
		.reservedNonBreaking = 0,
		.reservedBreaking = 0,
		.reserved = {},
	};

	__declspec(dllexport) bool SKSEPlugin_Query(const xse::Interface* a_skse, xse::PluginInfo* a_info) { return query(a_skse, a_info); }

	__declspec(dllexport) bool SKSEPlugin_Load(const xse::Interface* a_skse) { return on_load(a_skse, "SKSE", xse::kSkseMessaging); }

	__declspec(dllexport) bool F4SEPlugin_Query(const xse::Interface* a_f4se, xse::PluginInfo* a_info) { return query(a_f4se, a_info); }

	__declspec(dllexport) bool F4SEPlugin_Load(const xse::Interface* a_f4se) { return on_load(a_f4se, "F4SE", xse::kF4seMessaging); }

	// The short-lived helper: starts the watcher with the same arguments and exits, 0 when the watcher started.
	__declspec(dllexport) void CALLBACK RefreshSwitchSpawn(HWND, HINSTANCE, LPSTR a_arguments, int)
	{
		wchar_t             arguments[160] = {};
		PROCESS_INFORMATION process{};
		if (!a_arguments || MultiByteToWideChar(CP_ACP, 0, a_arguments, -1, arguments, static_cast<int>(std::size(arguments))) == 0 ||
			!run_helper(module_path(), kWatchEntry, arguments, process)) {
			ExitProcess(1);
		}
		CloseHandle(process.hThread);
		CloseHandle(process.hProcess);
		ExitProcess(0);
	}

	// The watcher: rundll32 calls this with the arguments start_watcher wrote. Waits for the game to end, for HDR Switch to finish, then puts the old rate back.
	__declspec(dllexport) void CALLBACK RefreshSwitchWatch(HWND, HINSTANCE, LPSTR a_arguments, int)
	{
		unsigned long pid = 0, low = 0, oldListed = 0, newListed = 0;
		long          high = 0;
		unsigned int  target = 0, numerator = 0, denominator = 0;
		char          loader[8] = {};
		if (!a_arguments || sscanf_s(a_arguments, "%lu %lu %ld %u %7s %u %u %lu %lu", &pid, &low, &high, &target, loader, static_cast<unsigned>(sizeof(loader)), &numerator, &denominator,
								&oldListed, &newListed) != 9) {
			return;
		}
		open_log(loader, false);
		use_real_pixels();
		Monitor monitor;
		monitor.adapter.LowPart = low;
		monitor.adapter.HighPart = high;
		monitor.target = target;
		const Rate old{ numerator, denominator };

		const HANDLE game = OpenProcess(SYNCHRONIZE, FALSE, pid);
		if (game) {
			// Exclusive fullscreen takes the monitor to a rate of its own as the window opens, so look for that while the game starts, then only wait: display queries can hitch a running game.
			Rate seen{};
			for (DWORD looked = 0; looked < kWatchStartMs && WaitForSingleObject(game, kWatchStepMs) == WAIT_TIMEOUT; looked += kWatchStepMs) {
				DisplayNow during;
				if (read_display(monitor, during) && (seen.denominator == 0 || !during.rate.same_as(seen))) {
					const bool first = seen.denominator == 0;
					seen = during.rate;
					if (during.whole != newListed) {
						log_line("rate now %.3f Hz, not %lu: exclusive fullscreen uses its own rate, or another program changed it", during.rate.value(), newListed);
					} else if (!first) {
						log_line("rate now %.3f Hz", during.rate.value());
					}
				}
			}
			WaitForSingleObject(game, INFINITE);
			CloseHandle(game);
		}
		log_line("game ended");

		// HDR Switch's turn first. It has its own watcher only when it turned HDR on.
		std::vector<Watcher> hdr;
		for (const auto& watcher : find_watchers(kHdrWatchEntry)) {
			if (watcher.gamePid == pid) {
				hdr.push_back(watcher);
			}
		}
		if (!hdr.empty()) {
			const auto started = GetTickCount64();
			const bool finished = wait_for(hdr, kHdrTurnMs);
			log_line(finished ? "HDR Switch finished after %llu ms" : "HDR Switch still running after %llu ms: going ahead", GetTickCount64() - started);
		}

		// A game in exclusive fullscreen may have picked its own rate, which the monitor leaves a moment after the game. Past that, a rate that is neither ours nor the old
		// one was set by someone else, and it stays.
		DisplayNow now;
		bool       read = false;
		const bool known = wait_until(
			[&] {
				read = read_display(monitor, now);
				return read && (now.whole == newListed || now.rate.same_as(old));
			},
			kReleaseMs);
		if (!read) {
			log_line("could not read the monitor; rate left as it is (a reboot brings the old rate back)");
			return;
		}
		if (!known) {
			log_line("rate is %.3f Hz, set by something else: leaving it", now.rate.value());
			return;
		}
		if (now.rate.same_as(old)) {
			log_line("rate already back at %.3f Hz", now.rate.value());
			return;
		}

		const auto started = GetTickCount64();
		const auto back = [&] { return read_display(monitor, now) && now.rate.same_as(old); };
		const auto exact = change_to_exact(monitor, old);
		if (exact != ERROR_SUCCESS || !wait_until(back, kSettleMs)) {   // Windows can say yes and pick another rate, so only the rate itself counts
			log_line("exact rate did not take (error %ld, now %.3f Hz); asking for %lu Hz as listed", exact, now.rate.value(), oldListed);
			const auto listed = change_to_listed(now.device, oldListed);
			if (listed != DISP_CHANGE_SUCCESSFUL) {
				log_line("Windows refused %lu Hz too (answer %ld); a reboot brings the old rate back", oldListed, listed);
				return;
			}
		}
		const bool restored = wait_until(back, kSettleMs);
		log_line(restored ? "rate back at %.3f Hz, %llu ms" : "rate did NOT go back: now %.3f Hz after %llu ms", now.rate.value(), GetTickCount64() - started);
	}
}
