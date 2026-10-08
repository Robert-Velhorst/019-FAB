// Preload before server imports so deployment validation sees production mode.
process.env.NODE_ENV = "production";
