"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { aliasedServerUrl, parseServerAliases } = require("../src/server_aliases");

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
});
