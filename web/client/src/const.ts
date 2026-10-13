export { COOKIE_NAME, ONE_YEAR_MS } from "@shared/const";

// Start on FAB so render-time links never create or expose browser credentials.
export const getLoginUrl = () => "/api/oauth/start";
