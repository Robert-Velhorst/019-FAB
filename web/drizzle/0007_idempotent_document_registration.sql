CREATE TEMPORARY TABLE `fab_document_canonical` AS
SELECT
	`source`,
	`sourceDocumentId`,
	MIN(`id`) AS `canonicalId`
FROM `bookkeeping_documents`
WHERE `sourceDocumentId` IS NOT NULL
GROUP BY `source`, `sourceDocumentId`
HAVING COUNT(*) > 1;
--> statement-breakpoint
UPDATE `review_items` AS `item`
INNER JOIN `bookkeeping_documents` AS `document` ON `item`.`documentId` = `document`.`id`
INNER JOIN `fab_document_canonical` AS `canonical`
	ON `canonical`.`source` = `document`.`source`
	AND `canonical`.`sourceDocumentId` = `document`.`sourceDocumentId`
SET `item`.`documentId` = `canonical`.`canonicalId`;
--> statement-breakpoint
UPDATE `routing_attempts` AS `attempt`
INNER JOIN `bookkeeping_documents` AS `document` ON `attempt`.`documentId` = `document`.`id`
INNER JOIN `fab_document_canonical` AS `canonical`
	ON `canonical`.`source` = `document`.`source`
	AND `canonical`.`sourceDocumentId` = `document`.`sourceDocumentId`
SET `attempt`.`documentId` = `canonical`.`canonicalId`;
--> statement-breakpoint
UPDATE `reconciliation_matches` AS `reconciliation`
INNER JOIN `bookkeeping_documents` AS `document` ON `reconciliation`.`documentId` = `document`.`id`
INNER JOIN `fab_document_canonical` AS `canonical`
	ON `canonical`.`source` = `document`.`source`
	AND `canonical`.`sourceDocumentId` = `document`.`sourceDocumentId`
SET `reconciliation`.`documentId` = `canonical`.`canonicalId`;
--> statement-breakpoint
UPDATE `bookkeeping_documents` AS `referencing`
INNER JOIN `bookkeeping_documents` AS `document` ON `referencing`.`duplicateOfDocumentId` = `document`.`id`
INNER JOIN `fab_document_canonical` AS `canonical`
	ON `canonical`.`source` = `document`.`source`
	AND `canonical`.`sourceDocumentId` = `document`.`sourceDocumentId`
SET `referencing`.`duplicateOfDocumentId` = `canonical`.`canonicalId`;
--> statement-breakpoint
UPDATE `audit_events` AS `event`
INNER JOIN `bookkeeping_documents` AS `document`
	ON BINARY `event`.`entityType` = BINARY 'bookkeeping_document'
	AND BINARY `event`.`entityId` = BINARY CAST(`document`.`id` AS CHAR)
INNER JOIN `fab_document_canonical` AS `canonical`
	ON `canonical`.`source` = `document`.`source`
	AND `canonical`.`sourceDocumentId` = `document`.`sourceDocumentId`
SET `event`.`entityId` = CAST(`canonical`.`canonicalId` AS CHAR);
--> statement-breakpoint
DELETE `document`
FROM `bookkeeping_documents` AS `document`
INNER JOIN `fab_document_canonical` AS `canonical`
	ON `canonical`.`source` = `document`.`source`
	AND `canonical`.`sourceDocumentId` = `document`.`sourceDocumentId`
WHERE `document`.`id` <> `canonical`.`canonicalId`;
--> statement-breakpoint
DROP TEMPORARY TABLE `fab_document_canonical`;
--> statement-breakpoint
DROP INDEX `idx_bookkeeping_docs_source` ON `bookkeeping_documents`;
--> statement-breakpoint
CREATE UNIQUE INDEX `uq_bookkeeping_docs_source_document`
	ON `bookkeeping_documents` (`source`, `sourceDocumentId`);
