import express, { type Express } from "express";
import fs from "fs";
import path from "path";

export function serveStatic(
  app: Express,
  distPath = path.resolve(import.meta.dirname, "public"),
) {
  if (!fs.existsSync(distPath)) {
    throw new Error(`Could not find the production build directory: ${distPath}`);
  }

  app.use(express.static(distPath, {
    setHeaders(res, filePath) {
      if (filePath.endsWith("index.html")) {
        res.setHeader("Cache-Control", "no-cache");
        return;
      }
      const assetsSegment = `${path.sep}assets${path.sep}`;
      res.setHeader(
        "Cache-Control",
        filePath.includes(assetsSegment)
          ? "public, max-age=31536000, immutable"
          : "public, max-age=86400",
      );
    },
  }));
  app.use((_req, res) => {
    res.setHeader("Cache-Control", "no-cache");
    // Anchor lookup to the public directory so hidden installation folders work.
    res.sendFile("index.html", { root: distPath });
  });
}
