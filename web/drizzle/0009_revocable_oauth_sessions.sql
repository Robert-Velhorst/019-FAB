CREATE TABLE `oauth_sessions` (
	`tokenHash` varchar(64) NOT NULL,
	`openId` varchar(64) NOT NULL,
	`expiresAt` timestamp NOT NULL,
	`revokedAt` timestamp,
	`createdAt` timestamp NOT NULL DEFAULT (now()),
	CONSTRAINT `oauth_sessions_tokenHash` PRIMARY KEY(`tokenHash`)
);
--> statement-breakpoint
CREATE INDEX `idx_oauth_sessions_expiry` ON `oauth_sessions` (`expiresAt`);
