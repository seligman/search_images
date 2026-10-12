/* Tests for the parts of static/duplicates.js that need no page: the
   choices read from the address and how dates and sizes are written.

   Run by tests/test_duplicates.py, or directly:
       node tests/js/duplicates_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var path = require("path");

var F = require(path.join(__dirname, "..", "..", "static", "duplicates.js"));

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

test("the address gives the page's choices", function () {
    assert.deepStrictEqual(F.choices({}), {kind: "all", min: 2, page: 1, stack: null});
    assert.deepStrictEqual(F.choices({kind: "exact", min: "5", page: "3", stack: "42"}),
                           {kind: "exact", min: 5, page: 3, stack: 42});
    // Anything the page does not offer is the default.
    assert.deepStrictEqual(F.choices({kind: "odd", min: "4", page: "-1", stack: "x"}),
                           {kind: "all", min: 2, page: 1, stack: null});
    assert.deepStrictEqual(F.choices(null), F.choices({}));
});

test("dates with and without a time", function () {
    var withTime = F.dateText("2019-07-04T10:15:01", "camera");
    assert.ok(/2019/.test(withTime) && /, 10:15$/.test(withTime), withTime);
    // A date from a file name without a time is stored as midnight.
    var dayOnly = F.dateText("2019-07-04T00:00:00", "file name");
    assert.ok(/2019/.test(dayOnly) && dayOnly.indexOf(":") < 0, dayOnly);
    assert.ok(/, 00:00$/.test(F.dateText("2019-07-04T00:00:00", "camera")));
    assert.ok(F.dateText("2019-07-04").indexOf(":") < 0);
    assert.strictEqual(F.dateText(""), "");
    assert.strictEqual(F.dateText("not a date"), "");
});

test("picture sizes", function () {
    assert.strictEqual(F.sizeText({width: 4032, height: 3024}),
                       "4032 \u00d7 3024 \u00b7 12.2 MP");
    assert.strictEqual(F.sizeText({width: 64, height: 48}), "64 \u00d7 48");
    assert.strictEqual(F.sizeText({width: 0, height: 48}), "Size not known");
});

test("what keeping pictures apart does, in words", function () {
    // 1 and 3 are one file (3's twin is 1, the first of them); 2 is another.
    var stack = {size: 3, contents: 2,
                 members: [{id: 1, twin: null}, {id: 2, twin: null}, {id: 3, twin: 1}]};
    assert.strictEqual(F.sameFiles(stack, stack.members[0]), 1);
    assert.strictEqual(F.sameFiles(stack, stack.members[2]), 1);
    assert.strictEqual(F.sameFiles(stack, stack.members[1]), 0);
    assert.strictEqual(F.apartText(stack, stack.members[1], "b.jpg"),
                       "b.jpg is no longer stacked with the others.");
    assert.strictEqual(F.apartText(stack, stack.members[0], "a.jpg"),
                       "a.jpg and its 1 identical file are no longer stacked with the others.");
    // Identical files stay together: the words do not say otherwise.
    assert.ok(/except identical files/.test(F.notDuplicatesText(stack)));
    var plain = {size: 3, contents: 3, members: [{id: 1}, {id: 2}, {id: 3}]};
    assert.strictEqual(F.notDuplicatesText(plain), "These 3 pictures are no longer stacked together.");
});

var failed = 0;
tests.forEach(function (item) {
    try {
        item.body();
        console.log("ok - " + item.name);
    } catch (error) {
        failed += 1;
        console.log("not ok - " + item.name + "\n" + (error && error.stack || error));
    }
});
if (failed) {
    console.log(failed + " of " + tests.length + " failed");
    process.exitCode = 1;
}
