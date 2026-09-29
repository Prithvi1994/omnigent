"use strict";

/**
 * Server aliases, persisted as settings.server_aliases: workspace origin → the
 * server URL the user picked. Recorded when Databricks sign-in moves an entered
 * URL (e.g. an account-level host) to its workspace's own host, so recents and
 * the server picker keep showing what the user picked.
 */

/**
 * @param {string} url
 * @returns {string | null}
 */
function originOf(url) {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

/**
 * The valid entries of a settings.server_aliases value (hand-edited settings
 * may hold anything).
 *
 * @param {unknown} value
 * @returns {Record<string, string>}
 */
function parseServerAliases(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value).filter(
      ([origin, url]) => originOf(origin) === origin && typeof url === "string",
    ),
  );
}

/**
 * The URL the user picked for `url`'s host, else `url` itself.
 *
 * @param {Record<string, string>} aliases From parseServerAliases.
 * @param {string} url
 * @returns {string}
 */
function aliasedServerUrl(aliases, url) {
  const origin = originOf(url);
  return origin !== null && Object.hasOwn(aliases, origin) ? aliases[origin] : url;
}

module.exports = { aliasedServerUrl, parseServerAliases };
