import { readFabDeployment } from "./deployment";

export const ENV = {
  ...readFabDeployment(process.env),
  appId: process.env.VITE_APP_ID ?? "",
  databaseUrl: process.env.DATABASE_URL ?? "",
  oAuthServerUrl: process.env.OAUTH_SERVER_URL ?? "",
  ownerOpenId: process.env.OWNER_OPEN_ID ?? "",
  forgeApiUrl: process.env.BUILT_IN_FORGE_API_URL ?? "",
  forgeApiKey: process.env.BUILT_IN_FORGE_API_KEY ?? "",
  fabBillingEnabled: process.env.FAB_BILLING_ENABLED
    ? ["1", "true", "yes", "on"].includes(process.env.FAB_BILLING_ENABLED.toLowerCase())
    : process.env.NODE_ENV === "test",
  fabLocalApiUrl: process.env.FAB_LOCAL_API_URL ?? "http://127.0.0.1:5001",
  fabInstanceRoot: process.env.FAB_INSTANCE_ROOT ?? "",
  fabLocalApiInsecureHosts: (process.env.FAB_LOCAL_API_INSECURE_HOSTS ?? "")
    .split(",")
    .map(value => value.trim().toLowerCase())
    .filter(Boolean),
  fabOperatorTrustDockerGateway: process.env.FAB_OPERATOR_TRUST_DOCKER_GATEWAY
    ?.toLowerCase() === "true",
  fabOperatorLocalMode: process.env.FAB_OPERATOR_LOCAL_MODE
    ? ["1", "true", "yes", "on"].includes(process.env.FAB_OPERATOR_LOCAL_MODE.toLowerCase())
    : process.env.NODE_ENV === "development",
};
