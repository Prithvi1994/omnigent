// Provider-visible copy for the pull request panel, keyed by the info payload's
// `provider` id. A host that predates the field serves GitHub.

import type { ComponentType } from "react";
import GithubMono from "@lobehub/icons/es/Github/components/Mono";
import { GitPullRequestIcon } from "lucide-react";

/** A provider glyph; lobehub brand icons and lucide icons both fit. */
export type GitProviderIcon = ComponentType<{ size?: number | string; className?: string }>;

/** What the panel shows for one git provider. In hints, `backticked` text renders as code. */
export interface GitProviderCopy {
  /** The info payload's `provider` id. */
  id: string;
  /** Display name, e.g. "GitHub". */
  label: string;
  Icon: GitProviderIcon;
  /** Shown before a PR number, e.g. "#" in "#123". */
  prNumberPrefix: string;
  /** Example URL in the link-a-PR input. */
  prUrlPlaceholder: string;
  /** Host left out of PR labels; null shows every host. */
  defaultHost: string | null;
  /** How to sign in on the host. */
  authHint: string;
  /** Name of the provider's CLI, as in "Install the GitHub CLI". */
  cliLabel: string;
  /** Hint when the upstream repo can't be reached; `authHint` when absent. */
  repoUnresolvedHint?: string;
}

const GITHUB: GitProviderCopy = {
  id: "github",
  label: "GitHub",
  Icon: GithubMono,
  prNumberPrefix: "#",
  prUrlPlaceholder: "https://github.com/owner/repo/pull/123",
  defaultHost: "github.com",
  authHint: "Run gh auth login on the host.",
  cliLabel: "GitHub CLI",
  repoUnresolvedHint:
    "Pick the account to use, or run `gh auth status` on the host to confirm the GitHub CLI is signed in.",
};

/** Copy for each known provider id. */
export const GIT_PROVIDERS: Readonly<Record<string, GitProviderCopy>> = { github: GITHUB };

/**
 * The copy for a provider id. No id (a host that predates `provider`) is GitHub;
 * an unknown id gets generic copy that uses the host's `auth.hint` when given.
 */
export function gitProviderCopy(id?: string, authHint?: string | null): GitProviderCopy {
  if (!id) return GITHUB;
  if (Object.hasOwn(GIT_PROVIDERS, id)) return GIT_PROVIDERS[id];
  return {
    id,
    label: id,
    Icon: GitPullRequestIcon,
    prNumberPrefix: "#",
    prUrlPlaceholder: "Pull request URL",
    defaultHost: null,
    authHint: authHint || `Sign in to ${id} on the host.`,
    cliLabel: `${id} CLI`,
  };
}
