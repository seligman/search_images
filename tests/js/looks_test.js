/* Tests for looks ("Looks like this", look:, search by an example picture) in
   static/search-core.js, the Export dialog's words for them
   (static/export-review.js) and the export's looks files (static/site.js),
   under Node (no packages needed).

   Run by tests/test_looks.py, or directly:
       node tests/js/looks_test.js
   Prints one line per test and exits with status 1 when any test fails.
   All records and vectors are made up here.
*/
"use strict";

var assert = require("assert");
var path = require("path");

var SearchCore = require(path.join(__dirname, "..", "..", "static", "search-core.js"));
var REVIEW = require(path.join(__dirname, "..", "..", "static", "export-review.js"));

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

var DIMS = 8;

// A signed-byte vector (unit length times 127) from numbers.
function byteVector(values) {
    var norm = Math.sqrt(values.reduce(function (sum, value) {
        return sum + value * value;
    }, 0));
    var out = new Int8Array(DIMS);
    values.forEach(function (value, k) {
        out[k] = Math.round(value / norm * 127);
    });
    return out;
}

/* A look vector whose cosine with axis 0 is cos: image vectors all share a
   large common part (here axes 0 and 7: two pictures at random come out at
   about 0.6), and differ by the rest. */
function look(cos, axis) {
    var values = [0, 0, 0, 0, 0, 0, 0, 0];
    values[0] = cos;
    values[axis || 1] = Math.sqrt(Math.max(0, 1 - cos * cos));
    return byteVector(values);
}

// The look vector set of {id: Int8Array}.
function lookSet(vectors, model) {
    var ids = Object.keys(vectors).map(Number);
    var data = new Int8Array(ids.length * DIMS);
    ids.forEach(function (id, k) {
        data.set(vectors[id], k * DIMS);
    });
    return {model: model || "l-0123456789ab", dims: DIMS, ids: ids, data: data};
}

/* An index of records with look vectors, among fillers (pictures that do not
   look like axis 0: cosines spread from 0.5 to 0.7, as image cosines of a
   varied library are). */
function lookIndex(records, vectors, fillers) {
    fillers = fillers === undefined ? 200 : fillers;
    for (var k = 0; k < fillers; k++) {
        var id = 10000 + k;
        records.push({i: id, t: "Filler picture " + k, dk: "2010-01-01", ds: 1});
        vectors[id] = look(0.5 + 0.2 * (k / Math.max(1, fillers - 1)), 5 + (k % 3));
    }
    var index = SearchCore.prepare(records);
    assert.strictEqual(index.setLooks(lookSet(vectors)), Object.keys(vectors).length);
    return index;
}

function sample() {
    return [
        {i: 1, t: "Red barn in the snow", g: ["barn", "snow"], dk: "2020-01-01", ds: 1},
        {i: 2, t: "Barn at dusk", g: ["barn"], dk: "2020-01-02", ds: 1},
        {i: 3, t: "Snowy field", g: ["snow", "field"], dk: "2019-01-03", ds: 1},
        {i: 4, t: "Birthday cake", g: ["cake"], dk: "2020-01-04", ds: 1},
        {i: 5, t: "No look yet", dk: "2020-01-05", ds: 1},
        {i: 6, t: "Hidden barn", g: ["barn"], dk: "2020-01-06", ds: 1, h: 1}
    ];
}

function sampleVectors() {
    return {1: look(1), 2: look(0.97, 2), 3: look(0.95, 3), 4: look(0.55, 4), 6: look(0.98, 4)};
}

test("look: and looks: are understood; -look: excludes the picture", function () {
    var parsed = SearchCore.parse("look:12 snow");
    var filters = parsed.terms.filter(function (term) {
        return term.kind === "filter";
    });
    assert.deepStrictEqual(filters[0].filter, {kind: "look", value: "12"});
    assert.deepStrictEqual(SearchCore.parse("looks:abc_1").terms[0].filter,
                           {kind: "look", value: "abc_1"});
    var bad = SearchCore.search([], "look:a/b", {});
    assert.ok(/look: takes a picture id/.test(bad.warnings.join(" ")), bad.warnings);
    // Nothing for meaning to embed in a look: query.
    assert.strictEqual(SearchCore.meaningText("look:5 beach"), "");
    var index = lookIndex(sample(), sampleVectors());
    assert.ok(index.search("barn -look:2").items.indexOf(2) < 0);
});

test("look: the pictures that look most like one picture, that one first", function () {
    var index = lookIndex(sample(), sampleVectors());
    var result = index.search("look:1");
    assert.deepStrictEqual(result.items, [1, 2, 3]);
    assert.strictEqual(result.sort, "relevance");
    assert.strictEqual(result.meaning.mode, "look");
    assert.deepStrictEqual(result.byMeaning, []);
    // Hidden pictures only when they are shown; words and filters still choose.
    assert.deepStrictEqual(index.search("look:1", {hidden: true}).items, [1, 6, 2, 3]);
    assert.deepStrictEqual(index.search("look:1 year:2020").items, [1, 2]);
    assert.deepStrictEqual(index.search("look:1 field").items, [3]);
    assert.deepStrictEqual(index.search("look:1", {sort: "oldest"}).items, [3, 1, 2]);
    var unknown = index.search("look:999");
    assert.deepStrictEqual(unknown.items, []);
    assert.ok(/no picture 999/.test(unknown.warnings.join(" ")), unknown.warnings);
    // A picture without a look vector: only itself, with a note.
    var none = index.search("look:5");
    assert.deepStrictEqual(none.items, [5]);
    assert.ok(/no look vector yet/.test(none.warnings.join(" ")), none.warnings);
    // Looks and meaning are apart: similar: knows nothing of the look vectors.
    assert.deepStrictEqual(index.search("similar:1").meaning.mode, "similar");
});

test("an example picture's vector finds the pictures that look like it", function () {
    var index = lookIndex(sample(), sampleVectors());
    var result = index.search("", {lookVector: look(0.99, 7)});
    assert.deepStrictEqual(result.items, [1, 2, 3]);
    assert.strictEqual(result.meaning.mode, "look");
    assert.deepStrictEqual(index.search("barn", {lookVector: look(0.99, 7)}).items, [1, 2]);
    // A vector of another size is not compared: nothing is found, with a note.
    var other = index.search("", {lookVector: new Int8Array(4)});
    assert.deepStrictEqual(other.items, []);
    assert.ok(/cannot be compared/.test(other.warnings.join(" ")), other.warnings);
    // A look: term wins over the example.
    assert.deepStrictEqual(index.search("look:4", {lookVector: look(1)}).items[0], 4);
});

test("Looks like this strip: closest by look, the picture's duplicates first", function () {
    var records = sample();
    records.push({i: 7, t: "Same barn again", dk: "2020-01-07", ds: 1, dg: 1});
    records[0].dg = 1;
    var vectors = sampleVectors();
    var index = lookIndex(records, vectors);
    assert.deepStrictEqual(index.similar(1, 8, {by: "look"}), [7, 2, 3]);
    assert.deepStrictEqual(index.similar(1, 2, {by: "look"}), [7, 2]);
    // Without a look vector: only the duplicates (no tag fallback, unlike by meaning).
    assert.deepStrictEqual(index.similar(5, 8, {by: "look"}), []);
    assert.ok(index.hasLook(1));
    assert.ok(!index.hasLook(5));
    assert.ok(!index.hasLook(999));
    // A new index (after an edit) has none until they are set again.
    assert.ok(!SearchCore.prepare(records).hasLook(1));
});

test("look vectors live apart from the meaning vectors", function () {
    var records = sample();
    var index = lookIndex(records, sampleVectors());
    // Meaning vectors where the closest to 1 is 4 (descriptions differ from looks).
    var meaning = {};
    records.forEach(function (rec) {
        meaning[rec.i] = look(rec.i === 4 ? 0.99 : rec.i === 1 ? 1 : 0.3, 3);
    });
    index.setVectors(lookSet(meaning));
    assert.strictEqual(index.search("look:1").items[1], 2);
    assert.strictEqual(index.similar(1, 1)[0], 4);
    assert.strictEqual(index.similar(1, 1, {by: "look"})[0], 2);
    // Taking the looks away leaves meaning as it was.
    index.setLooks(null);
    assert.ok(!index.hasLook(1));
    assert.strictEqual(index.similar(1, 1)[0], 4);
});

test("in a small set, the closest few count, judged by the looks' own spread", function () {
    // Nine pictures, as in a small export: no z can reach 3. Image cosines
    // run high (two pictures at random about 0.6 or more), so the closest
    // few count only when they are well above that.
    function small(cosines) {
        var records = [];
        var vectors = {};
        cosines.forEach(function (cos, k) {
            records.push({i: k + 1, t: "Picture " + (k + 1), dk: "2020-01-0" + (k + 1), ds: 1});
            vectors[k + 1] = look(cos, 1 + k % 6);
        });
        return lookIndex(records, vectors, 0);
    }
    assert.deepStrictEqual(small([1, 0.97, 0.95, 0.93, 0.92, 0.7, 0.66, 0.62, 0.6])
        .search("look:1").items, [1, 2, 3, 4, 5]);
    // Only pictures well above a typical pair of pictures: 0.75 is not.
    assert.deepStrictEqual(small([1, 0.75, 0.72, 0.7, 0.68, 0.66, 0.64, 0.62, 0.6])
        .search("look:1").items, [1]);
    // The spread the thresholds go by is the looks' own (not the meaning one's).
    var index = small([1, 0.97, 0.95, 0.93, 0.92, 0.7, 0.66, 0.62, 0.6]);
    var spread = index.lookView().pairSpread();
    assert.ok(spread.mean <= 0.65 + 1e-9 && spread.std <= 0.07 + 1e-9, JSON.stringify(spread));
});

test("a copy of the picture does not crowd out the pictures close to it", function () {
    // Picture 2 is a copy of 1 (another library's, not in its stack), and
    // three more pictures are close: a copy must not become what the closest
    // few are measured against, or take one of their places.
    function small(cosines, stacks) {
        var records = [];
        var vectors = {};
        cosines.forEach(function (cos, k) {
            var rec = {i: k + 1, t: "Picture " + (k + 1), dk: "2020-01-0" + (k + 1), ds: 1};
            if (stacks && stacks[k + 1] !== undefined) {
                rec.dg = stacks[k + 1];
            }
            records.push(rec);
            vectors[k + 1] = look(cos, 1 + k % 6);
        });
        return lookIndex(records, vectors, 0);
    }
    var cosines = [1, 0.995, 0.9, 0.88, 0.86, 0.7, 0.66, 0.62, 0.6];
    assert.deepStrictEqual(small(cosines).search("look:1").items, [1, 2, 3, 4, 5]);
    // The same picture dropped as an example: it and its copy first, then the close ones.
    var example = small(cosines).search("", {lookVector: look(0.999, 7)});
    assert.deepStrictEqual(example.items, [1, 2, 3, 4, 5]);
    assert.strictEqual(example.total, 5);
    // The strip too (the copy, then the close ones).
    assert.deepStrictEqual(small(cosines).similar(1, 8, {by: "look"}), [2, 3, 4, 5]);
    // A picture of the origin's own stack (a burst) is shown, but is not what
    // the others are measured against either.
    var stacked = small([1, 0.75, 0.93, 0.88, 0.86, 0.7, 0.66, 0.62, 0.6], {1: 1, 3: 1});
    assert.deepStrictEqual(stacked.search("look:1").items, [1, 3, 4, 5]);
    // An example whose copy is in a stack: that stack goes with it.
    var dropped = stacked.search("", {lookVector: look(0.999, 7)});
    assert.deepStrictEqual(dropped.items, [1, 3, 4, 5]);
    // Without copies nothing changes: one much closer picture still keeps
    // the ones that merely share a theme out.
    assert.deepStrictEqual(small([1, 0.96, 0.84, 0.83, 0.82, 0.7, 0.66, 0.62, 0.6])
        .search("look:1").items, [1, 2]);
});

test("decoded look vectors from an export's parts join like the meaning ones", function () {
    var parts = [
        {dims: 2, ids: ["a"], data: Buffer.from([1, 2]).toString("base64"), model: "l-1"},
        {dims: 2, ids: ["b"], data: Buffer.from([3, 4]).toString("base64"), model: "l-1"}
    ];
    var joined = SearchCore.joinVectors(parts.map(SearchCore.decodeVectors));
    assert.deepStrictEqual(joined.ids, ["a", "b"]);
    assert.deepStrictEqual(Array.from(joined.data), [1, 2, 3, 4]);
    assert.strictEqual(joined.model, "l-1");
});

test("the export review says what Looks like this adds", function () {
    var options = {looks: true};
    assert.strictEqual(REVIEW.looksText({exported: 0, looks: {vectors: 0, missing: 0}}, options,
                                        {}), "");
    assert.strictEqual(REVIEW.looksText({exported: 3, looks: {vectors: 3, missing: 0}},
                                        {looks: false}, {looks: 1050}), "");
    assert.ok(/No published picture has a look vector yet/.test(
        REVIEW.looksText({exported: 3, looks: {vectors: 0, missing: 3}}, options, {})));
    var all = REVIEW.looksText({exported: 3, looks: {vectors: 3, missing: 0}}, options,
                               {looks: 1050});
    assert.ok(/every published picture/.test(all), all);
    assert.ok(/about 350 bytes a picture/.test(all), all);
    var some = REVIEW.looksText({exported: 4, looks: {vectors: 3, missing: 1}}, options,
                                {looks: 1050});
    assert.ok(/3 of 4 pictures/.test(some) && /1 has no look vector yet/.test(some), some);
    assert.ok(REVIEW.OPTIONS.looks && /Looks like this/.test(REVIEW.OPTIONS.looks.label));
    // Choosing to leave out a picture with a look vector counts it out.
    var reply = {counts: {total: 2, exported: 2, by_reason: {}, looks: {vectors: 2, missing: 0}},
                 flagged: [{id: 1, reason: "document", reasons: ["document"], excluded: false,
                            bytes: 900, parts: {thumbs: 550, looks: 350}}],
                 size_estimate: 2000, size_parts: {thumbs: 1300, looks: 700}};
    var counted = REVIEW.recount(reply, {exclude_types: ["document"], include_ids: [],
                                         exclude_ids: [1]});
    assert.ok(counted.counts.looks.vectors <= 2);
});

/* ---------- Run ------------------------------------------------------------ */

var failures = 0;
tests.forEach(function (entry) {
    try {
        entry.body();
        console.log("ok - " + entry.name);
    } catch (error) {
        failures += 1;
        console.log("FAIL - " + entry.name);
        console.log(String(error && error.stack || error).split("\n").map(function (line) {
            return "    " + line;
        }).join("\n"));
    }
});
console.log((tests.length - failures) + " of " + tests.length + " passed");
process.exitCode = failures ? 1 : 0;
