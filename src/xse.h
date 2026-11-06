// The parts of the script extenders' plugin interface this plugin uses: what SKSE and F4SE read from a plugin and pass to it. Plain C++, no library.
//
// These are binary layouts, fixed by the loaders. The static_asserts pin each offset, so a wrong layout fails the build.
// Entry points each loader looks up:
//   SKSE for 1.5.97                  SKSEPlugin_Query, SKSEPlugin_Load
//   SKSE for 1.6.x and 1.7.x         SKSEPlugin_Version, SKSEPlugin_Load
//   F4SE for 1.10.163                F4SEPlugin_Query, F4SEPlugin_Load
//   F4SE for 1.10.980 and 1.11.x     F4SEPlugin_Version, F4SEPlugin_Load
#pragma once

#include <cstddef>
#include <cstdint>

namespace xse
{
	// SKSEInterface and F4SEInterface share this prefix (F4SE 0.7.1+ appends GetSaveFolderName, unused here).
	struct Interface
	{
		std::uint32_t xseVersion;
		std::uint32_t runtimeVersion;
		std::uint32_t editorVersion;
		std::uint32_t isEditor;
		void* (*QueryInterface)(std::uint32_t);
		std::uint32_t (*GetPluginHandle)();
		std::uint32_t (*GetReleaseIndex)();
		const void* (*GetPluginInfo)(const char*);
	};
	static_assert(offsetof(Interface, xseVersion) == 0x00);
	static_assert(offsetof(Interface, runtimeVersion) == 0x04);
	static_assert(offsetof(Interface, editorVersion) == 0x08);
	static_assert(offsetof(Interface, isEditor) == 0x0C);
	static_assert(offsetof(Interface, QueryInterface) == 0x10);
	static_assert(offsetof(Interface, GetPluginInfo) == 0x28);

	// QueryInterface ids of the messaging interface (SKSE: 5; F4SE: 1). The message types below are the same in both.
	constexpr std::uint32_t kSkseMessaging = 5;
	constexpr std::uint32_t kF4seMessaging = 1;
	constexpr std::uint32_t kPostPostLoad = 1;  // sent after every plugin has loaded

	struct Message
	{
		const char*   sender;
		std::uint32_t type;
		std::uint32_t dataLen;
		void*         data;
	};
	static_assert(offsetof(Message, type) == 0x08);
	static_assert(offsetof(Message, data) == 0x10);

	// SKSEMessagingInterface and F4SEMessagingInterface: the same layout.
	struct MessagingInterface
	{
		std::uint32_t interfaceVersion;
		bool (*RegisterListener)(std::uint32_t, const char*, void*);
	};
	static_assert(offsetof(MessagingInterface, RegisterListener) == 0x08);

	// The Query-era PluginInfo, the same in SKSE and F4SE.
	struct PluginInfo
	{
		std::uint32_t infoVersion;  // kVersion = 1
		const char*   name;
		std::uint32_t version;
	};
	static_assert(offsetof(PluginInfo, infoVersion) == 0x00);
	static_assert(offsetof(PluginInfo, name) == 0x08);
	static_assert(offsetof(PluginInfo, version) == 0x10);
	static_assert(sizeof(PluginInfo) == 0x18);

	// SKSEPlugin_Version (SKSE 2.1+, Skyrim 1.6.x / 1.7.x).
	struct SKSEPluginVersionData
	{
		enum : std::uint32_t
		{
			kVersion = 1,
			// versionIndependence
			kAddressLibraryPostAE = 1u << 0,
			kSignatures = 1u << 1,
			kStructsPost629 = 1u << 2,
			// versionIndependenceEx
			kNoStructUse = 1u << 0,
		};

		std::uint32_t dataVersion;
		std::uint32_t pluginVersion;
		char          pluginName[256];
		char          author[256];
		char          supportEmail[252];
		std::uint32_t versionIndependenceEx;
		std::uint32_t versionIndependence;
		std::uint32_t compatibleVersions[16];
		std::uint32_t xseMinimum;
	};
	static_assert(offsetof(SKSEPluginVersionData, dataVersion) == 0x000);
	static_assert(offsetof(SKSEPluginVersionData, pluginVersion) == 0x004);
	static_assert(offsetof(SKSEPluginVersionData, pluginName) == 0x008);
	static_assert(offsetof(SKSEPluginVersionData, author) == 0x108);
	static_assert(offsetof(SKSEPluginVersionData, supportEmail) == 0x208);
	static_assert(offsetof(SKSEPluginVersionData, versionIndependenceEx) == 0x304);
	static_assert(offsetof(SKSEPluginVersionData, versionIndependence) == 0x308);
	static_assert(offsetof(SKSEPluginVersionData, compatibleVersions) == 0x30C);
	static_assert(offsetof(SKSEPluginVersionData, xseMinimum) == 0x34C);
	static_assert(sizeof(SKSEPluginVersionData) == 0x350);

	// F4SEPlugin_Version (F4SE 0.7+, Fallout 4 1.10.980 and 1.11.x).
	struct F4SEPluginVersionData
	{
		enum : std::uint32_t
		{
			kVersion = 1,
			// addressIndependence
			kSignatures = 1u << 0,
			// structureIndependence
			kNoStructs = 1u << 0,
		};

		std::uint32_t dataVersion;
		std::uint32_t pluginVersion;
		char          pluginName[256];
		char          author[256];
		std::uint32_t addressIndependence;
		std::uint32_t structureIndependence;
		std::uint32_t compatibleVersions[16];
		std::uint32_t xseMinimum;
		std::uint32_t reservedNonBreaking;
		std::uint32_t reservedBreaking;
		std::uint8_t  reserved[512];
	};
	static_assert(offsetof(F4SEPluginVersionData, dataVersion) == 0x000);
	static_assert(offsetof(F4SEPluginVersionData, pluginVersion) == 0x004);
	static_assert(offsetof(F4SEPluginVersionData, pluginName) == 0x008);
	static_assert(offsetof(F4SEPluginVersionData, author) == 0x108);
	static_assert(offsetof(F4SEPluginVersionData, addressIndependence) == 0x208);
	static_assert(offsetof(F4SEPluginVersionData, structureIndependence) == 0x20C);
	static_assert(offsetof(F4SEPluginVersionData, compatibleVersions) == 0x210);
	static_assert(offsetof(F4SEPluginVersionData, xseMinimum) == 0x250);
	static_assert(offsetof(F4SEPluginVersionData, reservedNonBreaking) == 0x254);
	static_assert(offsetof(F4SEPluginVersionData, reservedBreaking) == 0x258);
	static_assert(offsetof(F4SEPluginVersionData, reserved) == 0x25C);
	static_assert(sizeof(F4SEPluginVersionData) == 0x45C);
}
