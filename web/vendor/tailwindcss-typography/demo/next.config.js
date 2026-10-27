const path = require('node:path')
const withMDX = require('@next/mdx')()

module.exports = withMDX({
  turbopack: { root: path.resolve(__dirname, '..') },
  pageExtensions: ['js', 'jsx', 'mdx'],
})
