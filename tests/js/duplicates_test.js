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
    assert.deepStrictEqual(F.choices({}), {kind: "all", min: 2, page: 1, stack: null, from: null});
    assert.deepStrictEqual(F.choices({kind: "exact", min: "5", page: "3", stack: "42"}),
                           {kind: "exact", min: 5, page: 3, stack: 42, from: null});
    // Anything the page does not offer is the default.
    assert.deepStrictEqual(F.choices({kind: "odd", min: "4", page: "-1", stack: "x", from: "x"}),
                           {kind: "all", min: 2, page: 1, stack: null, from: null});
    assert.deepStrictEqual(F.choices(null), F.choices({}));
    // "from" only goes with a stack.
    assert.strictEqual(F.choices({stack: "7", from: "same"}).from, "same");
    assert.strictEqual(F.choices({from: "same"}).from, null);
    // ... and back: what the address holds of them.
    assert.deepStrictEqual(F.address(F.choices({})),
                           {kind: null, min: null, page: null, stack: null, from: null});
    assert.deepStrictEqual(F.address(F.choices({kind: "same", min: "3", page: "2"})),
                           {kind: "same", min: "3", page: "2", stack: null, from: null});
});

test("a stack opened from Same picture, any date, and the way back", function () {
    var state = F.choices({kind: "same", min: "3", page: "4"});
    var query = F.stackQuery("?lib=lib_family1&kind=same&min=3&page=4", state, 17);
    var params = new URLSearchParams(query.slice(1));
    assert.strictEqual(query.charAt(0), "?");
    assert.strictEqual(params.get("lib"), "lib_family1");
    assert.strictEqual(params.get("stack"), "17");
    assert.strictEqual(params.get("from"), "same");
    assert.strictEqual(params.get("kind"), null);
    // The list's page and size stay in the address for the way back.
    var opened = F.choices({lib: "lib_family1", stack: params.get("stack"), from: "same",
                            min: params.get("min"), page: params.get("page")});
    assert.deepStrictEqual(opened, {kind: "all", min: 3, page: 4, stack: 17, from: "same"});
    assert.strictEqual(F.stackQuery("", F.choices({kind: "same"}), 5), "?stack=5&from=same");
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

test("the owner's marks of a picture, in words", function () {
    assert.strictEqual(F.marksText({favorite: true, rating: 4, albums: 2}),
                       "\u2605 Favorite \u00b7 4 stars \u00b7 in 2 albums");
    assert.strictEqual(F.marksText({rating: 1}), "1 star");
    assert.strictEqual(F.marksText({albums: 1}), "in an album");
    assert.strictEqual(F.marksText({}), "");
});

test("same picture, any date: the kind, titles and words", function () {
    assert.ok(F.KINDS.indexOf("same") >= 0);
    assert.strictEqual(F.LABELS.same, "Same picture, any date");
    assert.strictEqual(F.choices({kind: "same", stack: "7"}).kind, "same");
    assert.strictEqual(F.stackTitle({kind: "same", size: 3}), "3 pictures that look the same");
    assert.strictEqual(F.stackTitle({kind: "exact", size: 2}), "2 identical files");
    assert.strictEqual(F.stackTitle({kind: "similar", size: 1}), "1 similar picture");
    // Taken on other days: the first and the last day.
    var span = F.spanText({members: [{taken: "2021-06-02T10:00:00"}, {taken: ""},
                                     {taken: "2019-05-14T08:00:00"}]});
    assert.ok(/^Taken .*2019 to .*2021$/.test(span), span);
    assert.strictEqual(F.spanText({members: [{taken: "2019-05-14T08:00:00"},
                                             {taken: "2019-05-14T09:00:00"}]}), "");
    assert.strictEqual(F.spanText({members: [{taken: "2019-05-14"}]}), "");
    assert.strictEqual(F.stackedText({stacked: 5}), "Its stack of 5 (search page)");
    assert.strictEqual(F.stackedText({}), "");
});

test("same picture, any date: Not duplicates leaves the search page's stacks", function () {
    var stack = {kind: "same", size: 4, contents: 4,
                 members: [{id: 1, stack: 9}, {id: 2}, {id: 3, stack: 9}, {id: 4, stack: 12}]};
    // Pictures of one stack of the search page stay together (one alone needs nothing).
    assert.deepStrictEqual(F.togetherOf(stack), [[1, 3]]);
    assert.deepStrictEqual(F.togetherOf({members: [{id: 1}, {id: 2}]}), []);
    assert.ok(/no longer listed as the same picture/.test(F.notDuplicatesText(stack)));
    assert.ok(/stay as they are/.test(F.notDuplicatesText(stack)));
    assert.strictEqual(F.apartText(stack, stack.members[1], "b.jpg"),
                       "b.jpg is no longer listed with the others.");
    assert.strictEqual(F.undoneText(stack), "They are listed together again.");
    assert.strictEqual(F.undoneText({kind: "similar"}), "They are stacked again.");
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
