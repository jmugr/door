import { QuartzTransformerPlugin } from "../types"
import { Root, Paragraph, Text, Strong, Link, ThematicBreak } from "mdast"
import { BuildCtx } from "../../util/ctx"

export interface Options {
  displayFormat: "block" | "inline"
}

const defaultOptions: Options = {
  displayFormat: "block",
}

function unquoteValue(val: string): string {
  /**
   * Remove surrounding single or double quotes from the value, if present.
   * Leaves inner content (e.g., [[Name]]) intact.
   */
  const v = val.trim()
  if (v.length >= 2 && v[0] === v[-1] && (v[0] === '"' || v[0] === "'")) {
    return v.slice(1, -1).trim()
  }
  return v.trim()
}

function extractMetadata(frontmatter: any): { authors: string[]; sources: string[] } {
  const authors: string[] = []
  const sources: string[] = []

  if (!frontmatter) return { authors, sources }

  // Extract authors
  const authorKeys = Object.keys(frontmatter).filter(
    (key) => key.toLowerCase().includes("author"),
  )
  for (const key of authorKeys) {
    const val = frontmatter[key]
    if (Array.isArray(val)) {
      authors.push(...val.map((v) => unquoteValue(v.toString())))
    } else if (val) {
      authors.push(unquoteValue(val.toString()))
    }
  }

  // Extract sources
  const sourceKeys = Object.keys(frontmatter).filter((key) =>
    key.toLowerCase().includes("source"),
  )
  for (const key of sourceKeys) {
    const val = frontmatter[key]
    if (Array.isArray(val)) {
      sources.push(...val.map((v) => v.toString()))
    } else if (val) {
      sources.push(val.toString())
    }
  }

  return { authors, sources }
}

function createMetadataBlock(authors: string[], sources: string[]): Paragraph | null {
  if (authors.length === 0 && sources.length === 0) {
    return null
  }

  const children: (Text | Strong | Link)[] = []

  if (authors.length > 0) {
    children.push({
      type: "text",
      value: "Author(s): ",
    } as Text)

    // Create strong node with author text
    children.push({
      type: "strong",
      children: [
        {
          type: "text",
          value: authors.join(", "),
        },
      ],
    } as Strong)
  }

  if (sources.length > 0) {
    if (children.length > 0) {
      children.push({
        type: "text",
        value: " | ",
      } as Text)
    }

    children.push({
      type: "text",
      value: "Source(s): ",
    } as Text)

    // Create link nodes for sources
    const sourceLinks: (Text | Link)[] = []
    for (let i = 0; i < sources.length; i++) {
      const source = sources[i]
      const isUrl =
        source.toLowerCase().startsWith("http://") || source.toLowerCase().startsWith("https://")

      if (isUrl) {
        sourceLinks.push({
          type: "link",
          url: source,
          children: [
            {
              type: "text",
              value: source,
            },
          ],
        } as Link)
      } else {
        sourceLinks.push({
          type: "text",
          value: source,
        } as Text)
      }

      // Add commas between sources
      if (i < sources.length - 1) {
        sourceLinks.push({
          type: "text",
          value: ", ",
        } as Text)
      }
    }

    children.push(...sourceLinks)
  }

  return {
    type: "paragraph",
    children,
  }
}

export const MetadataInjector: QuartzTransformerPlugin<Partial<Options>> = (userOpts) => {
  const opts = { ...defaultOptions, ...userOpts }

  return {
    name: "MetadataInjector",
    markdownPlugins(_ctx: BuildCtx) {
      return [
        () => {
          return (tree: Root, file) => {
            // Only process files with publish: true
            const publish = file.data.frontmatter?.publish
            if (publish !== true && publish !== "true") {
              return
            }

            // Extract author and source from frontmatter
            const { authors, sources } = extractMetadata(file.data.frontmatter)

            // Create metadata block if we have data
            const metadataBlock = createMetadataBlock(authors, sources)
            if (!metadataBlock) {
              return
            }

            const separator: ThematicBreak = { type: "thematicBreak" }

            // Insert at the beginning of the tree with a separator
            if (tree.children && tree.children.length > 0) {
              tree.children = [metadataBlock, separator, ...tree.children]
            } else {
              tree.children = [metadataBlock, separator]
            }
          }
        },
      ]
    },
  }
}
