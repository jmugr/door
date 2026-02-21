import { FileTrieNode } from "../../util/fileTrie"
import { FullSlug, resolveRelative, simplifySlug } from "../../util/path"
import { ContentDetails } from "../../plugins/emitters/contentIndex"

type MaybeHTMLElement = HTMLElement | undefined

interface TagNode {
  name: string
  displayName: string
  isFolder: boolean
  slug: FullSlug
  children: Map<string, TagNode>
  items: Array<{ content: ContentDetails; dateMs: number }>
  count?: number
}

interface ParsedOptions {
  folderClickBehavior: "collapse" | "link"
  folderDefaultState: "collapsed" | "open"
  useSavedState: boolean
  sortFn: (a: FileTrieNode, b: FileTrieNode) => number
  filterFn: (node: FileTrieNode) => boolean
  mapFn: (node: FileTrieNode) => void
  order: "sort" | "filter" | "map"[]
}

type FolderState = {
  path: string
  collapsed: boolean
}

let currentExplorerState: Array<FolderState>

// Build tag-based tree structure
function buildTagBasedTree(entries: [FullSlug, ContentDetails][]): FileTrieNode {
  const makeTagNode = (name: string, displayName: string): TagNode => ({
    name,
    displayName,
    isFolder: true,
    slug: name as FullSlug,
    children: new Map(),
    items: [],
  })

  const collectRowsByPrefix = (prefix: string): Array<{
    group: string
    subtype: string
    content: ContentDetails
    dateMs: number
  }> => {
    const rows = []
    for (const [slug, content] of entries) {
      // Skip index file - it's the home page and shouldn't be categorized
      if (slug === "index" || slug.endsWith("/index")) {
        continue
      }

      const tags = content.tags || []
      const matchTags = tags.filter((tag) => {
        const tagStr = tag.toString().replace(/^#/, "")
        return tagStr.startsWith(`${prefix}/`)
      })

      const dateMs = content.date ? new Date(content.date).getTime() : 0

      if (matchTags.length === 0) {
        rows.push({ group: `${prefix}/none`, subtype: "", content, dateMs })
        continue
      }

      for (const tag of matchTags) {
        const tagStr = tag.toString().replace(/^#/, "")
        const parts = tagStr.split("/")
        const group = parts.length >= 2 ? `${prefix}/${parts[1]}` : `${prefix}/none`
        const subtype = parts.length > 2 ? parts.slice(2).join("/") : ""
        rows.push({ group, subtype, content, dateMs })
      }
    }
    return rows
  }

  const buildHierarchy = (rows: ReturnType<typeof collectRowsByPrefix>): TagNode[] => {
    const groupMap = new Map<string, TagNode>()
    for (const row of rows) {
      const group = row.group
      const node = groupMap.get(group) ?? makeTagNode(group, group)
      if (!groupMap.has(group)) {
        groupMap.set(group, node)
      }

      const subparts = row.subtype ? row.subtype.split("/") : []
      if (subparts.length === 0) {
        node.items.push({ content: row.content, dateMs: row.dateMs })
        continue
      }

      let cursor = node
      for (const part of subparts) {
        const child = cursor.children.get(part) ?? makeTagNode(part, part)
        if (!cursor.children.has(part)) {
          cursor.children.set(part, child)
        }
        cursor = child
      }
      cursor.items.push({ content: row.content, dateMs: row.dateMs })
    }

    return Array.from(groupMap.values())
  }

  const countNode = (node: TagNode): number => {
    let count = node.items.length
    for (const child of node.children.values()) {
      count += countNode(child)
    }
    node.count = count
    return count
  }

  const tagNodeToFileTrieNode = (
    tagNode: TagNode,
    segments: string[],
    parentContent?: ContentDetails,
  ): FileTrieNode<ContentDetails> => {
    const node = new FileTrieNode<ContentDetails>(segments, parentContent)
    node.isFolder = true
    node.displayName = tagNode.displayName

    // Add items (files) sorted by date
    const sortedItems = [...tagNode.items].sort((a, b) => b.dateMs - a.dateMs)
    for (const item of sortedItems) {
      // Use the actual file slug for proper linking
      const actualSlugSegments = item.content.slug.split("/")
      const fileNode = new FileTrieNode(actualSlugSegments, item.content)
      fileNode.isFolder = false
      node.children.push(fileNode)
    }

    // Add child folders sorted by count
    const sortedChildren = Array.from(tagNode.children.values()).sort(
      (a, b) => (b.count ?? 0) - (a.count ?? 0) || a.name.localeCompare(b.name),
    )

    for (const child of sortedChildren) {
      const childSegments = [...segments, child.name]
      const childNode = tagNodeToFileTrieNode(child, childSegments)
      node.children.push(childNode)
    }

    return node
  }

  // Build the root node
  const root = new FileTrieNode<ContentDetails>([], undefined)
  root.isFolder = true

  // Process "type" tags
  const typeRows = collectRowsByPrefix("type")
  const typeGroups = buildHierarchy(typeRows)
  typeGroups.forEach((node) => countNode(node))

  // Sort groups: /none last, then by count desc, then alphabetically
  typeGroups.sort((a, b) => {
    const aNone = a.name.endsWith("/none")
    const bNone = b.name.endsWith("/none")
    if (aNone !== bNone) return aNone ? 1 : -1
    return (b.count ?? 0) - (a.count ?? 0) || a.name.localeCompare(b.name)
  })

  // Create "By type" folder
  const typeFolder = new FileTrieNode<ContentDetails>(["type"], undefined)
  typeFolder.isFolder = true
  typeFolder.displayName = "By type"

  for (const group of typeGroups) {
    const displayName = group.name.startsWith("type/")
      ? group.name.slice(5)
      : group.name
    group.displayName = `${displayName} (${group.count ?? 0})`
    const groupNode = tagNodeToFileTrieNode(group, ["type", group.name])
    typeFolder.children.push(groupNode)
  }

  root.children.push(typeFolder)

  // Process "topic" tags
  const topicRows = collectRowsByPrefix("topic")
  const topicGroups = buildHierarchy(topicRows)
  topicGroups.forEach((node) => countNode(node))

  // Sort groups: /none last, then by count desc, then alphabetically
  topicGroups.sort((a, b) => {
    const aNone = a.name.endsWith("/none")
    const bNone = b.name.endsWith("/none")
    if (aNone !== bNone) return aNone ? 1 : -1
    return (b.count ?? 0) - (a.count ?? 0) || a.name.localeCompare(b.name)
  })

  // Create "By topic" folder
  const topicFolder = new FileTrieNode<ContentDetails>(["topic"], undefined)
  topicFolder.isFolder = true
  topicFolder.displayName = "By topic"

  for (const group of topicGroups) {
    const displayName = group.name.startsWith("topic/")
      ? group.name.slice(6)
      : group.name
    group.displayName = `${displayName} (${group.count ?? 0})`
    const groupNode = tagNodeToFileTrieNode(group, ["topic", group.name])
    topicFolder.children.push(groupNode)
  }

  root.children.push(topicFolder)

  return root
}

function toggleExplorer(this: HTMLElement) {
  const nearestExplorer = this.closest(".explorer") as HTMLElement
  if (!nearestExplorer) return
  const explorerCollapsed = nearestExplorer.classList.toggle("collapsed")
  nearestExplorer.setAttribute(
    "aria-expanded",
    nearestExplorer.getAttribute("aria-expanded") === "true" ? "false" : "true",
  )

  if (!explorerCollapsed) {
    // Stop <html> from being scrollable when mobile explorer is open
    document.documentElement.classList.add("mobile-no-scroll")
  } else {
    document.documentElement.classList.remove("mobile-no-scroll")
  }
}

function toggleFolder(evt: MouseEvent) {
  evt.stopPropagation()
  const target = evt.target as MaybeHTMLElement
  if (!target) return

  // Check if target was svg icon or button
  const isSvg = target.nodeName === "svg"

  // corresponding <ul> element relative to clicked button/folder
  const folderContainer = (
    isSvg
      ? // svg -> div.folder-container
        target.parentElement
      : // button.folder-button -> div -> div.folder-container
        target.parentElement?.parentElement
  ) as MaybeHTMLElement
  if (!folderContainer) return
  const childFolderContainer = folderContainer.nextElementSibling as MaybeHTMLElement
  if (!childFolderContainer) return

  childFolderContainer.classList.toggle("open")

  // Collapse folder container
  const isCollapsed = !childFolderContainer.classList.contains("open")
  setFolderState(childFolderContainer, isCollapsed)

  const currentFolderState = currentExplorerState.find(
    (item) => item.path === folderContainer.dataset.folderpath,
  )
  if (currentFolderState) {
    currentFolderState.collapsed = isCollapsed
  } else {
    currentExplorerState.push({
      path: folderContainer.dataset.folderpath as FullSlug,
      collapsed: isCollapsed,
    })
  }

  const stringifiedFileTree = JSON.stringify(currentExplorerState)
  localStorage.setItem("fileTree", stringifiedFileTree)
}

function createFileNode(currentSlug: FullSlug, node: FileTrieNode): HTMLLIElement {
  const template = document.getElementById("template-file") as HTMLTemplateElement
  const clone = template.content.cloneNode(true) as DocumentFragment
  const li = clone.querySelector("li") as HTMLLIElement
  const a = li.querySelector("a") as HTMLAnchorElement
  a.href = resolveRelative(currentSlug, node.slug)
  a.dataset.for = node.slug
  a.textContent = node.displayName

  if (currentSlug === node.slug) {
    a.classList.add("active")
  }

  return li
}

function createFolderNode(
  currentSlug: FullSlug,
  node: FileTrieNode,
  opts: ParsedOptions,
): HTMLLIElement {
  const template = document.getElementById("template-folder") as HTMLTemplateElement
  const clone = template.content.cloneNode(true) as DocumentFragment
  const li = clone.querySelector("li") as HTMLLIElement
  const folderContainer = li.querySelector(".folder-container") as HTMLElement
  const titleContainer = folderContainer.querySelector("div") as HTMLElement
  const folderOuter = li.querySelector(".folder-outer") as HTMLElement
  const ul = folderOuter.querySelector("ul") as HTMLUListElement

  const folderPath = node.slug
  folderContainer.dataset.folderpath = folderPath

  if (currentSlug === folderPath) {
    folderContainer.classList.add("active")
  }

  if (opts.folderClickBehavior === "link") {
    // Replace button with link for link behavior
    const button = titleContainer.querySelector(".folder-button") as HTMLElement
    const a = document.createElement("a")
    a.href = resolveRelative(currentSlug, folderPath)
    a.dataset.for = folderPath
    a.className = "folder-title"
    a.textContent = node.displayName
    button.replaceWith(a)
  } else {
    const span = titleContainer.querySelector(".folder-title") as HTMLElement
    span.textContent = node.displayName
  }

  // if the saved state is collapsed or the default state is collapsed
  const isCollapsed =
    currentExplorerState.find((item) => item.path === folderPath)?.collapsed ??
    opts.folderDefaultState === "collapsed"

  // if this folder is a prefix of the current path we
  // want to open it anyways
  const simpleFolderPath = simplifySlug(folderPath)
  const folderIsPrefixOfCurrentSlug =
    simpleFolderPath === currentSlug.slice(0, simpleFolderPath.length)

  if (!isCollapsed || folderIsPrefixOfCurrentSlug) {
    folderOuter.classList.add("open")
  }

  for (const child of node.children) {
    const childNode = child.isFolder
      ? createFolderNode(currentSlug, child, opts)
      : createFileNode(currentSlug, child)
    ul.appendChild(childNode)
  }

  return li
}

async function setupExplorer(currentSlug: FullSlug) {
  const allExplorers = document.querySelectorAll("div.explorer") as NodeListOf<HTMLElement>

  for (const explorer of allExplorers) {
    const dataFns = JSON.parse(explorer.dataset.dataFns || "{}")
    const opts: ParsedOptions = {
      folderClickBehavior: (explorer.dataset.behavior || "collapse") as "collapse" | "link",
      folderDefaultState: (explorer.dataset.collapsed || "collapsed") as "collapsed" | "open",
      useSavedState: explorer.dataset.savestate === "true",
      order: dataFns.order || ["filter", "map", "sort"],
      sortFn: new Function("return " + (dataFns.sortFn || "undefined"))(),
      filterFn: new Function("return " + (dataFns.filterFn || "undefined"))(),
      mapFn: new Function("return " + (dataFns.mapFn || "undefined"))(),
    }

    // Get folder state from local storage
    const storageTree = localStorage.getItem("fileTree")
    const serializedExplorerState = storageTree && opts.useSavedState ? JSON.parse(storageTree) : []
    const oldIndex = new Map<string, boolean>(
      serializedExplorerState.map((entry: FolderState) => [entry.path, entry.collapsed]),
    )

    const data = await fetchData
    const entries = [...Object.entries(data)] as [FullSlug, ContentDetails][]
    
    // Build tag-based tree instead of file path tree
    const trie = buildTagBasedTree(entries)

    // Apply functions in order
    for (const fn of opts.order) {
      switch (fn) {
        case "filter":
          if (opts.filterFn) trie.filter(opts.filterFn)
          break
        case "map":
          if (opts.mapFn) trie.map(opts.mapFn)
          break
        case "sort":
          if (opts.sortFn) trie.sort(opts.sortFn)
          break
      }
    }

    // Get folder paths for state management
    const folderPaths = trie.getFolderPaths()
    currentExplorerState = folderPaths.map((path) => {
      const previousState = oldIndex.get(path)
      return {
        path,
        collapsed:
          previousState === undefined ? opts.folderDefaultState === "collapsed" : previousState,
      }
    })

    const explorerUl = explorer.querySelector(".explorer-ul")
    if (!explorerUl) continue

    // Create and insert new content
    const fragment = document.createDocumentFragment()
    for (const child of trie.children) {
      const node = child.isFolder
        ? createFolderNode(currentSlug, child, opts)
        : createFileNode(currentSlug, child)

      fragment.appendChild(node)
    }
    // Insert content before the .overflow-end element to preserve scrolling functionality
    const overflowEnd = explorerUl.querySelector(".overflow-end")
    if (overflowEnd) {
      explorerUl.insertBefore(fragment, overflowEnd)
    } else {
      explorerUl.appendChild(fragment)
    }

    // restore explorer scrollTop position if it exists
    const scrollTop = sessionStorage.getItem("explorerScrollTop")
    if (scrollTop) {
      explorerUl.scrollTop = parseInt(scrollTop)
    } else {
      // try to scroll to the active element if it exists
      const activeElement = explorerUl.querySelector(".active")
      if (activeElement) {
        activeElement.scrollIntoView({ behavior: "smooth" })
      }
    }

    // Set up event handlers
    const explorerButtons = explorer.getElementsByClassName(
      "explorer-toggle",
    ) as HTMLCollectionOf<HTMLElement>
    for (const button of explorerButtons) {
      button.addEventListener("click", toggleExplorer)
      window.addCleanup(() => button.removeEventListener("click", toggleExplorer))
    }

    // Set up folder click handlers
    if (opts.folderClickBehavior === "collapse") {
      const folderButtons = explorer.getElementsByClassName(
        "folder-button",
      ) as HTMLCollectionOf<HTMLElement>
      for (const button of folderButtons) {
        button.addEventListener("click", toggleFolder)
        window.addCleanup(() => button.removeEventListener("click", toggleFolder))
      }
    }

    const folderIcons = explorer.getElementsByClassName(
      "folder-icon",
    ) as HTMLCollectionOf<HTMLElement>
    for (const icon of folderIcons) {
      icon.addEventListener("click", toggleFolder)
      window.addCleanup(() => icon.removeEventListener("click", toggleFolder))
    }
  }
}

document.addEventListener("prenav", async () => {
  // save explorer scrollTop position
  const explorer = document.querySelector(".explorer-ul")
  if (!explorer) return
  sessionStorage.setItem("explorerScrollTop", explorer.scrollTop.toString())
})

document.addEventListener("nav", async (e: CustomEventMap["nav"]) => {
  const currentSlug = e.detail.url
  await setupExplorer(currentSlug)

  // if mobile hamburger is visible, collapse by default
  for (const explorer of document.getElementsByClassName("explorer")) {
    const mobileExplorer = explorer.querySelector(".mobile-explorer")
    if (!mobileExplorer) return

    if (mobileExplorer.checkVisibility()) {
      explorer.classList.add("collapsed")
      explorer.setAttribute("aria-expanded", "false")

      // Allow <html> to be scrollable when mobile explorer is collapsed
      document.documentElement.classList.remove("mobile-no-scroll")
    }

    mobileExplorer.classList.remove("hide-until-loaded")
  }
})

window.addEventListener("resize", function () {
  // Desktop explorer opens by default, and it stays open when the window is resized
  // to mobile screen size. Applies `no-scroll` to <html> in this edge case.
  const explorer = document.querySelector(".explorer")
  if (explorer && !explorer.classList.contains("collapsed")) {
    document.documentElement.classList.add("mobile-no-scroll")
    return
  }
})

function setFolderState(folderElement: HTMLElement, collapsed: boolean) {
  return collapsed ? folderElement.classList.remove("open") : folderElement.classList.add("open")
}
