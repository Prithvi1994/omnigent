"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  aliasedServerUrl,
  aliasedWorkspaceOrigin,
  parseServerAliases,
  withConnectAlias,
} = require("../src/server_aliases");

const PICKED = "https://team.example.com/omnigent?o=123";
const WORKSPACE = "https://dbc-1234.cloud.databricks.com";

describe("server aliases", () => {
  it("keeps only origin → string entries from a settings value", () => {
    assert.deepEqual(parseServerAliases(undefined), {});
    assert.deepEqual(parseServerAliases(["x"]), {});
    assert.deepEqual(parseServerAliases("x"), {});
    assert.deepEqual(
      parseServerAliases({
        [WORKSPACE]: PICKED,
        "https://bad.example.com/path": PICKED, // not a bare origin
        "not a url": PICKED,
        "https://num.example.com": 42,
      }),
      { [WORKSPACE]: PICKED },
    );
  });

  it("maps any URL on an aliased host to the picked URL, and leaves others alone", () => {
    const aliases = { [WORKSPACE]: PICKED };
    assert.equal(aliasedServerUrl(aliases, `${WORKSPACE}/omnigent`), PICKED);
    assert.equal(aliasedServerUrl(aliases, WORKSPACE), PICKED);
    assert.equal(
      aliasedServerUrl(aliases, "https://other.example.com/"),
      "https://other.example.com/",
    );
    assert.equal(aliasedServerUrl(aliases, "not a url"), "not a url");
    // Inherited keys never count as aliases.
    assert.equal(aliasedServerUrl({}, "https://constructor"), "https://constructor");
  });

  it("finds the workspace host for an exact pick", () => {
    const aliases = { [WORKSPACE]: PICKED };
    assert.equal(aliasedWorkspaceOrigin(aliases, PICKED), WORKSPACE);
    assert.equal(aliasedWorkspaceOrigin(aliases, "https://team.example.com/"), null);
    assert.equal(aliasedWorkspaceOrigin({}, PICKED), null);
  });

  it("maps a moved host to the pick, and the latest host for a pick wins", () => {
    const other = "https://dbc-5678.cloud.databricks.com";
    const first = withConnectAlias({}, PICKED, `${WORKSPACE}/omnigent`);
    assert.deepEqual(first, { [WORKSPACE]: PICKED });
    // Same pick, another workspace chosen at sign-in: only the newest stays.
    assert.deepEqual(withConnectAlias(first, PICKED, `${other}/omnigent`), { [other]: PICKED });
  });

  it("drops a host's alias when it's connected to directly", () => {
    const aliases = {
      [WORKSPACE]: PICKED,
      "https://dbc-5678.cloud.databricks.com": "https://x.example.com/",
    };
    assert.deepEqual(withConnectAlias(aliases, `${WORKSPACE}/omnigent`, `${WORKSPACE}/omnigent`), {
      "https://dbc-5678.cloud.databricks.com": "https://x.example.com/",
    });
    assert.equal(withConnectAlias(aliases, PICKED, "not a url"), aliases);
  });
});
