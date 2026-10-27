// MDX 3 parses Markdown inside explicit JSX paragraphs. Flatten only the
// redundant paragraph wrapper, preserving every original text/inline node.
// This keeps the publisher's Markdown samples intact and produces valid HTML.
module.exports = function remarkValidParagraphs() {
  return function transform(tree) {
    function walk(node) {
      if (!Array.isArray(node.children)) return
      for (const child of node.children) walk(child)
      if (node.type === 'mdxJsxFlowElement' && node.name === 'p') {
        const blocks = node.children.filter((child) => child.type !== 'text' || child.value.trim())
        if (blocks.length === 1 && blocks[0].type === 'paragraph') {
          node.children = blocks[0].children
        }
      }
    }
    walk(tree)
  }
}
