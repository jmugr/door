import path from "path"
import { readFile } from "fs/promises"
import { FilePath } from "./path"

// ![[target]], ![[target|size]], ![[target#section]]
const WIKILINK_EMBED = /!\[\[([^\]|#^\n]+)/g
// ![alt](target) and ![alt](<target with spaces>)
const MARKDOWN_EMBED = /!\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^)\s]+)/g
const FENCED_CODE = /^(`{3,}|~{3,})[^\n]*\n[\s\S]*?^\1[ \t]*$/gm
const INLINE_CODE = /`[^`\n]*`/g

/** Raw embed targets in a note's markdown, skipping code blocks and inline code */
export function extractEmbedTargets(markdown: string): string[] {
  const text = markdown.replace(FENCED_CODE, "").replace(INLINE_CODE, "")
  const targets: string[] = []
  for (const match of text.matchAll(WIKILINK_EMBED)) targets.push(match[1])
  for (const match of text.matchAll(MARKDOWN_EMBED)) {
    targets.push(match[1].replace(/^<|>$/g, ""))
  }
  return targets
}

function normalizeTarget(raw: string): string {
  let target = raw.trim()
  try {
    target = decodeURIComponent(target)
  } catch {
    // keep the raw text if it isn't valid percent-encoding
  }
  return target.replace(/\\/g, "/").replace(/^\.?\//, "")
}

/**
 * Resolves embed targets against a pool of embed-only files the way Obsidian does:
 * an exact vault path first, then the shortest path whose trailing segments match.
 */
export class EmbedIndex {
  private byPath: Set<string>
  private byName = new Map<string, FilePath[]>()

  constructor(files: FilePath[]) {
    this.byPath = new Set(files)
    for (const fp of files) {
      const name = path.posix.basename(fp).toLowerCase()
      const list = this.byName.get(name) ?? []
      list.push(fp)
      this.byName.set(name, list)
    }
    for (const list of this.byName.values()) {
      list.sort((a, b) => a.split("/").length - b.split("/").length || a.localeCompare(b))
    }
  }

  resolve(raw: string): FilePath | undefined {
    const target = normalizeTarget(raw)
    if (target === "" || /^[a-z][a-z0-9+.-]*:/i.test(target)) return undefined
    if (this.byPath.has(target)) return target as FilePath
    const candidates = this.byName.get(path.posix.basename(target).toLowerCase()) ?? []
    const suffix = "/" + target.toLowerCase()
    return candidates.find(
      (fp) => !target.includes("/") || ("/" + fp.toLowerCase()).endsWith(suffix),
    )
  }
}

/** Embed-only files embedded by any of the given notes (absolute or cwd-relative paths) */
export async function collectEmbeddedFiles(
  notePaths: FilePath[],
  index: EmbedIndex,
): Promise<Set<FilePath>> {
  const found = new Set<FilePath>()
  await Promise.all(
    notePaths.map(async (fp) => {
      const markdown = await readFile(fp, "utf-8")
      for (const target of extractEmbedTargets(markdown)) {
        const resolved = index.resolve(target)
        if (resolved !== undefined) found.add(resolved)
      }
    }),
  )
  return found
}
