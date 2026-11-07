import test, { describe } from "node:test"
import assert from "node:assert"
import { EmbedIndex, extractEmbedTargets } from "./embeds"
import { FilePath } from "./path"

describe("extractEmbedTargets", () => {
  test("finds wikilink and markdown embeds", () => {
    const md = [
      "![[Pasted image 1.png]]",
      "![[photo.jpg|300]] and ![[doc.pdf#page=2]]",
      '![alt](<Assets/my file.png>) ![x](Assets/b%20c.png "title")',
      "[[not-an-embed.png]] and [link](other.png)",
    ].join("\n")
    assert.deepStrictEqual(extractEmbedTargets(md), [
      "Pasted image 1.png",
      "photo.jpg",
      "doc.pdf",
      "Assets/my file.png",
      "Assets/b%20c.png",
    ])
  })

  test("ignores fenced and inline code", () => {
    const md = "```md\n![[secret.png]]\n```\n`![[inline.png]]`\n![[shown.png]]"
    assert.deepStrictEqual(extractEmbedTargets(md), ["shown.png"])
  })
})

describe("EmbedIndex", () => {
  const index = new EmbedIndex([
    "! ADMIN/Assets/Pasted image 1.png",
    "! ADMIN/Assets/b c.png",
    "deep/folder/dup.png",
    "dup.png",
  ] as FilePath[])

  test("resolves by name, path suffix, and percent-encoding", () => {
    assert.strictEqual(index.resolve("Pasted image 1.png"), "! ADMIN/Assets/Pasted image 1.png")
    assert.strictEqual(index.resolve("Assets/b%20c.png"), "! ADMIN/Assets/b c.png")
    assert.strictEqual(index.resolve("! ADMIN/Assets/b c.png"), "! ADMIN/Assets/b c.png")
  })

  test("prefers the shortest path for duplicate names", () => {
    assert.strictEqual(index.resolve("dup.png"), "dup.png")
    assert.strictEqual(index.resolve("folder/dup.png"), "deep/folder/dup.png")
  })

  test("ignores external URLs and unknown files", () => {
    assert.strictEqual(index.resolve("https://example.com/dup.png"), undefined)
    assert.strictEqual(index.resolve("missing.png"), undefined)
  })
})
