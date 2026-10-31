// Legacy wire keys are retained only for connecting to existing installations.
export const ENGINE_TOKEN_HEADER = 'x-lora1-token';
export const LEGACY_TOKEN_HEADER = 'x-solstice-token';
export function engineAuthHeaders(token) {
  return token ? { [ENGINE_TOKEN_HEADER]: token, [LEGACY_TOKEN_HEADER]: token } : {};
}
