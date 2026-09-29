"use strict";

const { normalizeUrl } = require("./url");

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
 * A server URL's identity, as recents compare it: the root URL, keeping a
 * workspace `?o=` selector. Null for an invalid URL.
 *
 * @param {string} url
 * @returns {string | null}
 */
function serverIdentity(url) {
  try {
    return normalizeUrl(url);
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

/**
 * The workspace origin whose alias is `url` (the URL the user picked, in any
 * equivalent form), else null. Stored sign-in tokens are keyed by that host.
 *
 * @param {Record<string, string>} aliases From parseServerAliases.
 * @param {string} url
 * @returns {string | null}
 */
function aliasedWorkspaceOrigin(aliases, url) {
  const identity = serverIdentity(url);
  if (identity === null) return null;
  return (
    Object.keys(aliases).find((origin) => serverIdentity(aliases[origin]) === identity) ?? null
  );
}

/**
 * Aliases after an explicit connect to `picked` landed on `connected`. When
 * sign-in moved hosts, the connected host maps to the pick, replacing any older
 * host for the same pick so the latest choice wins. A direct connect to a host
 * drops that host's alias.
 *
 * @param {Record<string, string>} aliases From parseServerAliases.
 * @param {string} picked
 * @param {string} connected
 * @returns {Record<string, string>}
 */
function withConnectAlias(aliases, picked, connected) {
  const connectedOrigin = originOf(connected);
  if (connectedOrigin === null) return aliases;
  const identity = serverIdentity(picked);
  const next = Object.fromEntries(
    Object.entries(aliases).filter(
      ([origin, pick]) => origin !== connectedOrigin && serverIdentity(pick) !== identity,
    ),
  );
  if (originOf(picked) !== connectedOrigin) next[connectedOrigin] = picked;
  return next;
}

module.exports = {
  aliasedServerUrl,
  aliasedWorkspaceOrigin,
  parseServerAliases,
  serverIdentity,
  withConnectAlias,
};
