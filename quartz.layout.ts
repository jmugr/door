import { PageLayout, SharedLayout } from "./quartz/cfg"
import * as Component from "./quartz/components"

// components shared across all pages
export const sharedPageComponents: SharedLayout = {
  head: Component.Head(),
  header: [],
  afterBody: [Component.TagList()],
  footer: Component.Footer({
    socialLinks: {
      label: "Follow via:",
      items: {
        "Changelog emails": "mailto:jr.auto.generated.emails@gmail.com?subject=Sign%20Up%20for%20Changelog%20Emails",
        Bluesky: "https://bsky.app/profile/jmugair.bsky.social",
        X: "https://x.com/jmugair",
        Reddit: "https://www.reddit.com/user/jmugr/",
        GitHub: "https://github.com/jmugr/door",
      },
    },
  }),
}

// components for pages that display a single page (e.g. a single note)
export const defaultContentPageLayout: PageLayout = {
  beforeBody: [
    Component.ConditionalRender({
      component: Component.Breadcrumbs(),
      condition: (page) => page.fileData.slug !== "index",
    }),
    Component.ContentMeta(),
  ],
  left: [
    Component.PageTitle(),
    Component.MobileOnly(Component.Spacer()),
    Component.Flex({
      components: [
        {
          Component: Component.Search(),
          grow: true,
        },
        { Component: Component.Darkmode() },
        { Component: Component.ReaderMode() },
      ],
    }),
    Component.Explorer({
      title: "Contents",
      folderClickBehavior: "collapse",
    }),
  ],
  right: [
    Component.Graph({
      localGraph: { showTags: false },
      globalGraph: { showTags: false },
    }),
    Component.DesktopOnly(Component.TableOfContents()),
    Component.Backlinks(),
  ],
}

// components for pages that display lists of pages  (e.g. tags or folders)
export const defaultListPageLayout: PageLayout = {
  beforeBody: [Component.Breadcrumbs(), Component.ArticleTitle(), Component.ContentMeta()],
  left: [
    Component.PageTitle(),
    Component.MobileOnly(Component.Spacer()),
    Component.Flex({
      components: [
        {
          Component: Component.Search(),
          grow: true,
        },
        { Component: Component.Darkmode() },
      ],
    }),
    Component.Explorer({
      title: "Contents",
      folderClickBehavior: "collapse",
    }),
  ],
  right: [],
}
