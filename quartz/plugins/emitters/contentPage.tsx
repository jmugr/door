import path from "path"
import { QuartzEmitterPlugin } from "../types"
import { QuartzComponentProps } from "../../components/types"
import HeaderConstructor from "../../components/Header"
import BodyConstructor from "../../components/Body"
import { pageResources, renderPage } from "../../components/renderPage"
import { FullPageLayout } from "../../cfg"
import { pathToRoot } from "../../util/path"
import { defaultContentPageLayout, sharedPageComponents } from "../../../quartz.layout"
import { Content } from "../../components"
import { styleText } from "util"
import { write } from "./helpers"
import { BuildCtx } from "../../util/ctx"
import { Node } from "unist"
import { StaticResources } from "../../util/resources"
import { QuartzPluginData } from "../vfile"
import { visit } from "unist-util-visit"
import { Root, Element } from "hast"
import isAbsoluteUrl from "is-absolute-url"

function createTransclusionLookup(
  publishedFiles: QuartzPluginData[],
  transcludeOnlyFiles: QuartzPluginData[] = [],
): QuartzPluginData[] {
  const bySlug = new Map<string, QuartzPluginData>()

  for (const file of publishedFiles) {
    if (typeof file.slug === "string") {
      bySlug.set(file.slug, file)
    }
  }

  for (const file of transcludeOnlyFiles) {
    if (typeof file.slug === "string" && !bySlug.has(file.slug)) {
      bySlug.set(file.slug, file)
    }
  }

  return Array.from(bySlug.values())
}

async function processContent(
  ctx: BuildCtx,
  tree: Node,
  fileData: QuartzPluginData,
  allFiles: QuartzPluginData[],
  opts: FullPageLayout,
  resources: StaticResources,
) {
  const slug = fileData.slug!
  const cfg = ctx.cfg.configuration
  
  // Build a set of all published file slugs for quick lookup
  const publishedSlugs = new Set<string>()
  for (const file of allFiles) {
    if (file.slug) {
      publishedSlugs.add(file.slug as string)
    }
  }

  // Mark links to unpublished files as broken (same styling as disableBrokenWikilinks)
  // This works in conjunction with the disableBrokenWikilinks option in the OFM transformer
  visit(tree as Root, "element", (node: Element) => {
    if (node.tagName === "a" && node.properties && typeof node.properties.href === "string") {
      const href = node.properties.href
      const classes = (node.properties.className ?? []) as string[]
      
      // Skip tag links
      if (classes.includes("tag-link")) {
        return
      }
      
      // Skip external links
      if (isAbsoluteUrl(href, { httpOnly: false })) {
        return
      }

      // Get the slug this link is pointing to
      const dataSlug = node.properties["data-slug"]
      if (typeof dataSlug === "string") {
        // Check if the linked file is published
        if (!publishedSlugs.has(dataSlug)) {
          // Mark as broken by adding the class and removing href
          if (!classes.includes("broken")) {
            classes.push("broken")
            node.properties.className = classes
          }
          // Remove href to make it non-clickable (same as disableBrokenWikilinks)
          delete node.properties.href
        }
      }
    }
  })

  const externalResources = pageResources(pathToRoot(slug), resources)
  const transcludeOnlyFiles = (ctx.transcludeOnly ?? []).map((c) => c[1].data)
  const transcludeFiles = createTransclusionLookup(allFiles, transcludeOnlyFiles)
  const componentData: QuartzComponentProps = {
    ctx,
    fileData,
    externalResources,
    cfg,
    children: [],
    tree,
    allFiles,
    transcludeFiles,
  }

  const content = renderPage(cfg, slug, componentData, opts, externalResources)
  return write({
    ctx,
    content,
    slug,
    ext: ".html",
  })
}

export const ContentPage: QuartzEmitterPlugin<Partial<FullPageLayout>> = (userOpts) => {
  const opts: FullPageLayout = {
    ...sharedPageComponents,
    ...defaultContentPageLayout,
    pageBody: Content(),
    ...userOpts,
  }

  const { head: Head, header, beforeBody, pageBody, afterBody, left, right, footer: Footer } = opts
  const Header = HeaderConstructor()
  const Body = BodyConstructor()

  return {
    name: "ContentPage",
    getQuartzComponents() {
      return [
        Head,
        Header,
        Body,
        ...header,
        ...beforeBody,
        pageBody,
        ...afterBody,
        ...left,
        ...right,
        Footer,
      ]
    },
    async *emit(ctx, content, resources) {
      const allFiles = content.map((c) => c[1].data)
      let containsIndex = false

      for (const [tree, file] of content) {
        const slug = file.data.slug!
        if (slug === "index") {
          containsIndex = true
        }

        // only process home page, non-tag pages, and non-index pages
        if (slug.endsWith("/index") || slug.startsWith("tags/")) continue
        yield processContent(ctx, tree, file.data, allFiles, opts, resources)
      }

      if (!containsIndex) {
        console.log(
          styleText(
            "yellow",
            `\nWarning: you seem to be missing an \`index.md\` home page file at the root of your \`${ctx.argv.directory}\` folder (\`${path.join(ctx.argv.directory, "index.md")} does not exist\`). This may cause errors when deploying.`,
          ),
        )
      }
    },
    async *partialEmit(ctx, content, resources, changeEvents) {
      const allFiles = content.map((c) => c[1].data)

      // find all slugs that changed or were added
      const changedSlugs = new Set<string>()
      for (const changeEvent of changeEvents) {
        if (!changeEvent.file) continue
        if (changeEvent.type === "add" || changeEvent.type === "change") {
          changedSlugs.add(changeEvent.file.data.slug!)
        }
      }

      for (const [tree, file] of content) {
        const slug = file.data.slug!
        if (!changedSlugs.has(slug)) continue
        if (slug.endsWith("/index") || slug.startsWith("tags/")) continue

        yield processContent(ctx, tree, file.data, allFiles, opts, resources)
      }
    },
  }
}
