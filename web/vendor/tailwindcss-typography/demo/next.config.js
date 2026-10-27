const path = require('node:path')
const withMDX = require('@next/mdx')({
  options: {
    remarkPlugins: [require.resolve('./remark-valid-paragraphs.cjs')],
  },
})

module.exports = withMDX({
  turbopack: { root: path.resolve(__dirname, '..') },
  pageExtensions: ['js', 'jsx', 'mdx'],
})
