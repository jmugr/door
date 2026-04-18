import { QuartzConfig } from "./quartz/cfg"
import * as Plugin from "./quartz/plugins"

/**
 * Quartz 4 Configuration
 *
 * See https://quartz.jzhao.xyz/configuration for more information.
 */
const config: QuartzConfig = {
  configuration: {
    pageTitle: "JR's Garage",
    pageTitleSuffix: "",
    enableSPA: true,
    enablePopovers: true,
    analytics: {
      provider: "plausible",
    },
    locale: "en-US",
    baseUrl: "jmugr.github.io/door/",
    ignorePatterns: [
      "private", 
      "templates", 
      ".obsidian",
      "**/*.patch",
      "**/*.pdf",
      "**/*.canvas",
      "Attachments/!(public-|Public-)*.*",
    ],
    defaultDateType: "modified",
    theme: {
      fontOrigin: "googleFonts",
      cdnCaching: true,
      typography: {
        header: "Merriweather",
        body: "Inter",
        code: "IBM Plex Mono",
      },
      colors: {
        lightMode: {
          light: "#ffffff",
          lightgray: "#e6e6e6",
          gray: "#b3b3b3",
          darkgray: "#3d3d3d",
          dark: "#000000",
          secondary: "#111111",
          tertiary: "#E85D04",
          highlight: "rgba(0, 0, 0, 0.08)",
          textHighlight: "#cccccc88",
        },
        darkMode: {
          light: "#000000",
          lightgray: "#2b2b2b",
          gray: "#666666",
          darkgray: "#d9d9d9",
          dark: "#ffffff",
          secondary: "#f2f2f2",
          tertiary: "#E85D04",
          highlight: "rgba(255, 255, 255, 0.12)",
          textHighlight: "#66666688",
        },
      },
    },
  },
  plugins: {
    transformers: [
      Plugin.FrontMatter(),
      Plugin.MetadataInjector(),
      Plugin.CreatedModifiedDate({
        priority: ["git", "filesystem"],
      }),
      Plugin.SyntaxHighlighting({
        theme: {
          light: "github-light",
          dark: "github-dark",
        },
        keepBackground: false,
      }),
      Plugin.ObsidianFlavoredMarkdown({ 
        enableInHtmlEmbed: false,
        disableBrokenWikilinks: true,
      }),
      Plugin.GitHubFlavoredMarkdown(),
      Plugin.TableOfContents(),
      Plugin.CrawlLinks({ markdownLinkResolution: "shortest" }),
      Plugin.Description(),
      Plugin.Latex({ renderEngine: "katex" }),
    ],
    filters: [
      Plugin.ExplicitPublish(),
    ],
    emitters: [
      Plugin.AliasRedirects(),
      Plugin.ComponentResources(),
      Plugin.ContentPage(),
      Plugin.FolderPage(),
      Plugin.TagPage(),
      Plugin.ContentIndex({
        enableSiteMap: true,
        enableRSS: true,
      }),
      Plugin.Assets(),
      Plugin.Static(),
      Plugin.Favicon(),
      Plugin.NotFoundPage(),
      // Comment out CustomOgImages to speed up build time
      // Plugin.CustomOgImages(),
    ],
  },
}

export default config
