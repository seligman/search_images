/* Tests for static/search-core.js under Node (no packages needed).

   Run by tests/test_search_core.py, or directly:
       node tests/js/search_core_test.js
   Prints one line per test and exits with status 1 when any test fails.
   All records are made up here.
*/
"use strict";

var assert = require("assert");
var path = require("path");

var SearchCore = require(path.join(__dirname, "..", "..", "static", "search-core.js"));

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

/* ---------- Helpers -------------------------------------------------------- */

// A small, seeded random number generator (mulberry32), so every run
// builds the same records.
function randomSource(seed) {
    var state = seed >>> 0;
    return function () {
        state = (state + 0x6d2b79f5) >>> 0;
        var value = state;
        value = Math.imul(value ^ (value >>> 15), value | 1);
        value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
        return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
    };
}

function search(records, query, options) {
    return SearchCore.search(records, query, options || {});
}

function items(records, query, options) {
    return search(records, query, options).items;
}

function counts(result, group) {
    var out = {};
    result.facets[group].forEach(function (entry) {
        out[entry.value] = entry.count;
    });
    return out;
}

function values(result, group) {
    return result.facets[group].map(function (entry) {
        return entry.value;
    });
}

function activeWords(query) {
    var out = [];
    SearchCore.parse(query).clauses.forEach(function (clause) {
        clause.terms.forEach(function (term) {
            out.push(term.words.join(" "));
        });
    });
    return out;
}

function onlyTerm(query, options) {
    var parsed = SearchCore.parse(query, options);
    assert.strictEqual(parsed.clauses.length, 1, "one clause for " + query);
    assert.strictEqual(parsed.clauses[0].terms.length, 1, "one term for " + query);
    return parsed.clauses[0].terms[0];
}

function median(list) {
    var sorted = list.slice().sort(function (a, b) {
        return a - b;
    });
    return sorted[Math.floor(sorted.length / 2)];
}

function now() {
    var time = process.hrtime();
    return time[0] * 1000 + time[1] / 1e6;
}

/* ---------- Normalisation -------------------------------------------------- */

test("normalize lowers case, removes accents and apostrophes", function () {
    assert.strictEqual(SearchCore.normalize("Cr\u00e8me Br\u00fbl\u00e9e"), "creme brulee");
    assert.strictEqual(SearchCore.normalize("Stra\u00dfe"), "strasse");
    assert.strictEqual(SearchCore.normalize("DON'T"), "dont");
    assert.strictEqual(SearchCore.normalize("don\u2019t"), "dont");
    assert.strictEqual(SearchCore.normalize("\uff21\uff22\uff23"), "abc");
    assert.strictEqual(SearchCore.normalize("A\u030angstro\u0308m"), "angstrom");
    assert.strictEqual(SearchCore.normalize(null), "");
    assert.strictEqual(SearchCore.normalize(42), "42");
});

test("tokens split on everything but letters and digits", function () {
    assert.deepStrictEqual(SearchCore.tokens("Hello, world! It's 10:30"),
                           ["hello", "world", "its", "10", "30"]);
    assert.deepStrictEqual(SearchCore.tokens("\u00c5ngstr\u00f6m caf\u00e9 \u2014 t-shirt"),
                           ["angstrom", "cafe", "t", "shirt"]);
    assert.deepStrictEqual(SearchCore.tokens("\u6771\u4eac\u30bf\u30ef\u30fc"),
                           ["\u6771", "\u4eac", "\u30bf", "\u30ef", "\u30fc"]);
    assert.deepStrictEqual(SearchCore.tokens("\u041c\u043e\u0441\u043a\u0432\u0430 2019"),
                           ["\u043c\u043e\u0441\u043a\u0432\u0430", "2019"]);
    assert.deepStrictEqual(SearchCore.tokens("\ud83d\udc36\ud83c\udf89"), []);
    assert.deepStrictEqual(SearchCore.tokens(""), []);
});

test("browsers without Unicode property escapes split words the same way", function () {
    // Load the engine where new RegExp() rejects \p{...}, as older engines do.
    var fs = require("fs");
    var vm = require("vm");
    var source = fs.readFileSync(path.join(__dirname, "..", "..", "static", "search-core.js"),
                                 "utf8");
    var rejected = 0;
    var sandbox = {
        module: {exports: {}},
        RegExp: function (pattern, flags) {
            if (/\\p[{]/.test(pattern)) {
                rejected += 1;
                throw new SyntaxError("no property escapes here");
            }
            return new RegExp(pattern, flags);
        }
    };
    vm.runInNewContext(source, sandbox);
    var Old = sandbox.module.exports;
    // (Its arrays come from another realm: compare them as JSON.)
    function sameJson(actual, expected) {
        assert.strictEqual(JSON.stringify(actual), JSON.stringify(expected));
    }
    assert.strictEqual(rejected, 2);
    var text = "Caf\u00e9 \u00c5ngstr\u00f6m, \u041c\u043e\u0441\u043a\u0432\u0430 10:30 " +
        "\u6771\u4eac don't \u00bd";
    sameJson(Old.tokens(text), SearchCore.tokens(text));
    var query = "cafe \u043c\u043e\u0441\u043a\u0432\u0430 dont";
    sameJson(Old.highlights(text, query), SearchCore.highlights(text, query));
    sameJson(Old.search([{i: 1, t: text}], "angstrom").items, [1]);
});

test("plural folding", function () {
    var cases = {cats: "cat", boxes: "box", berries: "berry", dogs: "dog", beaches: "beach",
                 glasses: "glass", ties: "tie", children: "child", photos: "photo",
                 glass: "glass", bus: "bus", gas: "gas", cactus: "cactus", tennis: "tennis",
                 "1990s": "1990s", dog: "dog", cookie: "cookie"};
    Object.keys(cases).forEach(function (word) {
        assert.strictEqual(SearchCore.fold(word), cases[word], word);
    });
    // Other possible singulars are searched too.
    assert.ok(SearchCore.forms("cookies").indexOf("cookie") >= 0);
    assert.ok(SearchCore.forms("tomatoes").indexOf("tomato") >= 0);
    assert.ok(SearchCore.forms("buses").indexOf("bus") >= 0);
    assert.ok(SearchCore.forms("knives").indexOf("knife") >= 0);
    assert.ok(SearchCore.forms("menus").indexOf("menu") >= 0);
    assert.deepStrictEqual(SearchCore.forms("dress"), ["dress"]);
    // Never a form of two letters: "toes" is not "to", "goes" not "go".
    assert.deepStrictEqual(SearchCore.forms("toes"), ["toes", "toe"]);
    assert.deepStrictEqual(SearchCore.forms("goes"), ["goes", "goe"]);
    assert.ok(SearchCore.forms("heroes").indexOf("hero") >= 0);
    var records = [{i: 1, t: "Baby toes", dk: "2020-01-01"},
                   {i: 2, d: "Tulips stretch to the horizon.", dk: "2020-01-02"}];
    assert.deepStrictEqual(items(records, "toes"), [1]);
    assert.deepStrictEqual(SearchCore.highlights("stretch to the toe", "toes"), [[15, 18]]);
});

/* ---------- Parser --------------------------------------------------------- */

test("parse never throws, whatever it is given", function () {
    var random = randomSource(7);
    var pieces = ["\"", "\u201c", "-", "--", " ", "  ", "OR", "or", "NOT", "AND", ":", "tag:",
                  "type:", "year:", "before:", "is:", "has:", "lib:", "people:", "x:", "|",
                  "caf\u00e9", "\u6771\u4eac", "\ud83d\udc36", "\ud83d", "don't", "'", "`",
                  "red", "car", "2019", "2015-2019", "\u0000", "\n", "\t", "\\", "*", "(", ")",
                  "\u0301", "\uffff", "constructor", "__proto__", "toString", "-\"", "\"\""];
    var records = [{i: 1, t: "red car", g: ["constructor"], dk: "2019-01-01"},
                   {i: 2, d: "caf\u00e9 \u6771\u4eac", h: 1}];
    for (var round = 0; round < 3000; round++) {
        var query = "";
        var length = 1 + Math.floor(random() * 8);
        for (var k = 0; k < length; k++) {
            query += pieces[Math.floor(random() * pieces.length)];
            if (random() < 0.5) {
                query += " ";
            }
        }
        var parsed = SearchCore.parse(query);
        assert.ok(Array.isArray(parsed.clauses), query);
        var result = search(records, query, {group: true, hidden: true,
                                             filters: {type: ["photo"], has: ["text"]}});
        assert.ok(Array.isArray(result.items), query);
        SearchCore.highlights("red car caf\u00e9 \u6771\u4eac don't", query);
        SearchCore.suggest(records, query);
    }
    [undefined, null, 5, {}, [], "a".repeat(5000), "\"".repeat(99), "-".repeat(99),
     "tag:".repeat(99), "x ".repeat(500)].forEach(function (query) {
        assert.ok(Array.isArray(SearchCore.parse(query).clauses));
        assert.ok(Array.isArray(search(records, query).items));
    });
});

test("words, phrases and exclusions", function () {
    assert.deepStrictEqual(activeWords("cat dog"), ["cat", "dog"]);
    var phrase = onlyTerm("\"Red Car\"");
    assert.strictEqual(phrase.kind, "phrase");
    assert.deepStrictEqual(phrase.words, ["red", "car"]);
    assert.ok(phrase.quoted && phrase.closed && !phrase.prefix);
    var smart = onlyTerm("\u201cred car\u201d");
    assert.deepStrictEqual(smart.words, ["red", "car"]);
    assert.ok(smart.closed);
    var open = onlyTerm("\"red ca", {typing: true});
    assert.ok(!open.closed && open.prefix, "an unclosed phrase at the end is being typed");
    assert.ok(!onlyTerm("\"red ca").prefix, "not while nothing is being typed");
    assert.ok(onlyTerm("-dog").neg);
    var excluded = onlyTerm("-\"red car\"");
    assert.ok(excluded.neg && excluded.kind === "phrase");
    assert.ok(onlyTerm("--dog").neg);
    assert.strictEqual(SearchCore.parse("-").clauses.length, 0);
    assert.strictEqual(SearchCore.parse(" - ").clauses.length, 0);
    assert.deepStrictEqual(activeWords("cat -"), ["cat"]);
    assert.ok(SearchCore.parse("cat NOT dog").clauses[1].terms[0].neg);
    assert.strictEqual(SearchCore.parse("\"\"").clauses.length, 0);
    assert.strictEqual(SearchCore.parse("!!! ???").clauses.length, 0);
    var hyphen = onlyTerm("t-shirt");
    assert.strictEqual(hyphen.kind, "phrase");
    assert.ok(!hyphen.quoted);
});

test("OR between terms", function () {
    var parsed = SearchCore.parse("cat OR dog beach");
    assert.strictEqual(parsed.clauses.length, 2);
    assert.deepStrictEqual(parsed.clauses[0].terms.map(function (term) {
        return term.words[0];
    }), ["cat", "dog"]);
    assert.strictEqual(SearchCore.parse("cat OR dog OR bird").clauses[0].terms.length, 3);
    assert.strictEqual(SearchCore.parse("cat | dog").clauses.length, 1);
    // A lower-case "or" between two words is OR too; elsewhere a small word.
    var lower = SearchCore.parse("cat or dog");
    assert.strictEqual(lower.clauses.length, 1);
    assert.deepStrictEqual(lower.clauses[0].terms.map(function (term) {
        return term.words[0];
    }), ["cat", "dog"]);
    assert.strictEqual(SearchCore.parse("black Or white beach").clauses.length, 2);
    assert.deepStrictEqual(activeWords("cat or"), ["cat"]);
    assert.deepStrictEqual(activeWords("or cat"), ["cat"]);
    assert.deepStrictEqual(activeWords("OR cat"), ["cat"]);
    assert.deepStrictEqual(activeWords("cat OR"), ["cat"]);
    assert.deepStrictEqual(activeWords("cat OR OR dog"), ["cat", "dog"]);
    assert.strictEqual(SearchCore.parse("cat OR OR dog").clauses.length, 1);
});

test("field filters", function () {
    var tag = onlyTerm("Tag:Beach");
    assert.strictEqual(tag.field, "tag");
    assert.deepStrictEqual(tag.words, ["beach"]);
    var quoted = onlyTerm("tag:\"ice cream\"");
    assert.strictEqual(quoted.field, "tag");
    assert.deepStrictEqual(quoted.words, ["ice", "cream"]);
    assert.ok(onlyTerm("-text:exit").neg);
    // An unknown field is ordinary text.
    var unknown = onlyTerm("foo:bar");
    assert.strictEqual(unknown.field, null);
    assert.deepStrictEqual(unknown.words, ["foo", "bar"]);
    assert.deepStrictEqual(onlyTerm("10:30").words, ["10", "30"]);
    // A field with nothing after it yet is skipped.
    assert.strictEqual(SearchCore.parse("tag:").clauses.length, 0);
    assert.deepStrictEqual(activeWords("beach tag:"), ["beach"]);
});

test("filter values and their other words", function () {
    function filter(query) {
        return onlyTerm(query).filter;
    }
    assert.deepStrictEqual(filter("type:screenshots"), {kind: "photo_type", value: "screenshot"});
    assert.deepStrictEqual(filter("type:Receipt"), {kind: "document_type", value: "receipt"});
    assert.deepStrictEqual(filter("type:\"card or id\""),
                           {kind: "document_type", value: "card_or_id"});
    assert.deepStrictEqual(filter("type:id"), {kind: "document_type", value: "card_or_id"});
    assert.deepStrictEqual(filter("type:drawing"), {kind: "photo_type", value: "artwork"});
    assert.deepStrictEqual(filter("type:360"), {kind: "360"});
    assert.deepStrictEqual(filter("is:receipt"), {kind: "document_type", value: "receipt"});
    assert.deepStrictEqual(filter("is:screenshot"), {kind: "photo_type", value: "screenshot"});
    assert.deepStrictEqual(filter("people:4"), {kind: "people", value: "3-5"});
    assert.deepStrictEqual(filter("people:0"), {kind: "people", value: "none"});
    assert.deepStrictEqual(filter("people:25"), {kind: "people", value: "crowd"});
    assert.deepStrictEqual(filter("people:6-10"), {kind: "people", value: "6-10"});
    assert.deepStrictEqual(filter("people:nobody"), {kind: "people", value: "none"});
    assert.deepStrictEqual(filter("people:any"), {kind: "people_any"});
    assert.deepStrictEqual(filter("year:2019"), {kind: "year", from: 2019, to: 2019});
    assert.deepStrictEqual(filter("year:2019-2015"), {kind: "year", from: 2015, to: 2019});
    assert.deepStrictEqual(filter("year:2015.."), {kind: "year", from: 2015, to: 9999});
    assert.deepStrictEqual(filter("before:2019-5"), {kind: "before", key: "2019-05"});
    assert.deepStrictEqual(filter("after:2019/05/14"), {kind: "after", key: "2019-05-14"});
    assert.deepStrictEqual(filter("has:location"), {kind: "has", what: "gps"});
    assert.deepStrictEqual(filter("has:animals"), {kind: "has", what: "animals"});
    assert.deepStrictEqual(filter("is:duplicates"), {kind: "is", what: "duplicate"});
    assert.deepStrictEqual(filter("lib:Phone"), {kind: "lib", value: "Phone"});
    assert.deepStrictEqual(filter("lib:\"My Photos\""), {kind: "lib", value: "My Photos"});
    ["type:selfie", "people:seven", "year:19", "before:2019-13", "has:nothing",
     "is:great"].forEach(function (query) {
        var parsed = SearchCore.parse(query);
        assert.ok(parsed.terms[0].invalid, query);
        assert.strictEqual(parsed.warnings.length, 1, query);
    });
});

test("the last word is a prefix only while it is typed", function () {
    var typing = {typing: true};
    assert.ok(onlyTerm("sun", typing).prefix);
    assert.ok(!onlyTerm("sun ", typing).prefix);
    assert.ok(!onlyTerm("-sun", typing).prefix);
    assert.ok(!onlyTerm("\"sun\"", typing).prefix);
    assert.ok(onlyTerm("tag:sun", typing).prefix);
    assert.ok(!onlyTerm("type:scr", typing).prefix);
    var parsed = SearchCore.parse("beach sun", typing);
    assert.ok(!parsed.clauses[0].terms[0].prefix);
    assert.ok(parsed.clauses[1].terms[0].prefix);
    assert.ok(parsed.typing);
    // Without typing (Enter, a link, a reload, a chip, the map) never.
    assert.ok(!onlyTerm("sun").prefix);
    assert.ok(!SearchCore.parse("beach sun").clauses[1].terms[0].prefix);
    // The two are told apart by the parse cache.
    assert.ok(onlyTerm("sun", typing).prefix);
});

test("small words are skipped unless they are all there is", function () {
    var parsed = SearchCore.parse("dog on the beach");
    assert.deepStrictEqual(activeWords("dog on the beach"), ["dog", "beach"]);
    assert.deepStrictEqual(parsed.sequence, ["dog", "on", "the", "beach"]);
    assert.deepStrictEqual(activeWords("the"), ["the"]);
    assert.deepStrictEqual(activeWords("\"the who\""), ["the who"]);
    assert.deepStrictEqual(activeWords("the -dog"), ["dog"]);
});

test("filler words are skipped like small words", function () {
    assert.deepStrictEqual(activeWords("my dog"), ["dog"]);
    assert.deepStrictEqual(activeWords("pictures of cats"), ["cats"]);
    assert.deepStrictEqual(activeWords("show me receipts"), ["receipts"]);
    assert.deepStrictEqual(activeWords("photo of a receipt"), ["receipt"]);
    // Nouns like "photos" stay when only fillers are left; alone, any word stays.
    assert.deepStrictEqual(activeWords("my photos"), ["photos"]);
    assert.deepStrictEqual(activeWords("me"), ["me"]);
    assert.deepStrictEqual(activeWords("type:photo my"), [""], "only the filter is left");
    // Quoted, excluded or in a field they count.
    assert.deepStrictEqual(activeWords("\"my dog\""), ["my dog"]);
    assert.deepStrictEqual(activeWords("dog -photo"), ["dog", "photo"]);
    // The words in order, without skipped ones at either end.
    assert.deepStrictEqual(SearchCore.parse("photo of a dog on the beach").sequence,
                           ["dog", "on", "the", "beach"]);
    assert.strictEqual(SearchCore.parse("my dog").sequence, null);
    var records = [{i: 1, t: "Dog on a couch", dk: "2020-01-01"},
                   {i: 2, g: ["receipt"], ty: "document", dk: "2020-01-02"}];
    assert.deepStrictEqual(items(records, "pictures of my dog"), [1]);
    assert.deepStrictEqual(items(records, "show me the receipts"), [2]);
});

test("\"no people\" is people:none", function () {
    var term = onlyTerm("no people");
    assert.strictEqual(term.kind, "filter");
    assert.deepStrictEqual(term.filter, {kind: "people", value: "none"});
    var parsed = SearchCore.parse("beach without people");
    assert.strictEqual(parsed.clauses.length, 2);
    assert.deepStrictEqual(parsed.clauses[1].terms[0].filter, {kind: "people", value: "none"});
    // Elsewhere "no" and "people" are words.
    assert.deepStrictEqual(activeWords("no parking"), ["no", "parking"]);
    assert.deepStrictEqual(activeWords("people dancing"), ["people", "dancing"]);
});

test("accents and apostrophes in queries", function () {
    assert.deepStrictEqual(onlyTerm("Caf\u00e9").words, ["cafe"]);
    assert.deepStrictEqual(onlyTerm("don't").words, ["dont"]);
    assert.deepStrictEqual(onlyTerm("don\u2019t").words, ["dont"]);
});

/* ---------- Matching and ranking ------------------------------------------- */

test("people and person find every picture with people", function () {
    var records = [
        {i: 1, t: "Family picnic on the beach", pp: "3-5", pd: ["man", "woman"],
         dk: "2020-01-01"},
        {i: 2, t: "Busy market", pp: "crowd", dk: "2020-01-02"},
        {i: 3, t: "Empty beach", pp: "none", dk: "2020-01-03"},
        {i: 4, t: "Old beach photo", dk: "2020-01-04"}
    ];
    assert.deepStrictEqual(items(records, "people").sort(), [1, 2]);
    assert.deepStrictEqual(items(records, "person").sort(), [1, 2]);
    assert.deepStrictEqual(items(records, "crowd"), [2]);
    assert.deepStrictEqual(items(records, "beach -people").sort(), [3, 4]);
    assert.deepStrictEqual(items(records, "beach no people"), [3]);
});

test("a picture about the words beats one that mentions them in passing", function () {
    var records = [
        {i: 1, t: "Woman in a red dress on a city street",
         d: "A woman in a red dress walks past a parked red car on a busy street.",
         g: ["woman", "dress", "street"], c: ["red"], dk: "2022-10-10"},
        {i: 2, t: "Red sports car on a mountain road", g: ["red", "car", "sports car"],
         v: ["sports car"], c: ["red"], dk: "2021-09-09"},
        {i: 3, d: "A red car in a parking lot.", g: ["car", "red"], dk: "2015-05-05", v4: 1}
    ];
    assert.deepStrictEqual(items(records, "red car")[0], 2);
    assert.deepStrictEqual(items(records, "red car ")[0], 2);
    // Quoted, the words must be together (and the bonus is large).
    assert.deepStrictEqual(items(records, "\"red car\""), [3, 1]);
    // The whole-query bonus counts whole words only, never a word start.
    var carpet = [
        {i: 1, t: "Celebrities posing on the red carpet", dk: "2024-01-01"},
        {i: 2, t: "Red car", dk: "2019-01-01"}
    ];
    assert.deepStrictEqual(items(carpet, "red car", {typing: true}), [2]);
    // (While "ca" is typed both are word starts: equal, newest first.)
    assert.deepStrictEqual(items(carpet, "red ca", {typing: true}), [1, 2]);
    // In the title it counts more than in the description.
    var places = [
        {i: 1, d: "A dog waits at the golden gate of the garden.", g: ["dog", "gate"],
         dk: "2024-01-01"},
        {i: 2, t: "Golden Gate Park conservatory", g: ["park", "golden gate"], dk: "2018-01-01"}
    ];
    assert.deepStrictEqual(items(places, "golden gate"), [2, 1]);
});

test("a year counts as much as a title word", function () {
    var records = [
        {i: 1, t: "Grocery store receipt", g: ["receipt"], dk: "2019-03-14"},
        {i: 2, t: "Warehouse club receipt", g: ["receipt"], o: "MEMBER SINCE 2019",
         dk: "2022-06-01"},
        {i: 3, t: "Beach", dk: "2019-07-01"}
    ];
    assert.deepStrictEqual(items(records, "receipt 2019"), [1, 2]);
    assert.deepStrictEqual(items(records, "2019"), [3, 1, 2]);
    // Month names are weak words: "may" is also an ordinary word.
    var months = [{i: 1, t: "May pole dance", dk: "2020-06-01"}, {i: 2, dk: "2020-05-01"}];
    assert.deepStrictEqual(items(months, "may"), [1, 2]);
});

test("a title match beats a description match", function () {
    var records = [
        {i: 1, t: "Sunset over the lake", dk: "2018-01-01"},
        {i: 2, d: "A dog playing in the park at sunset.", dk: "2022-01-01"},
        {i: 3, f: "2019/sunset", dk: "2023-01-01"}
    ];
    assert.deepStrictEqual(items(records, "sunset"), [1, 2, 3]);
    // Fields weigh title 10 > tags 7 > subjects 6 > scene 4 > description 3 > folder 1.
    var fields = [
        {i: 1, f: "beach", dk: "2025-01-01"},
        {i: 2, d: "beach", dk: "2024-01-01"},
        {i: 3, sc: "beach", dk: "2023-01-01"},
        {i: 4, s: ["beach"], dk: "2022-01-01"},
        {i: 5, g: ["beach"], dk: "2021-01-01"},
        {i: 6, t: "Beach", dk: "2020-01-01"}
    ];
    assert.deepStrictEqual(items(fields, "beach "), [6, 5, 4, 3, 2, 1]);
});

test("a phrase found word for word ranks first", function () {
    var records = [
        {i: 1, d: "A car with red seats.", dk: "2023-01-01"},
        {i: 2, d: "A red car parked by the road.", dk: "2019-01-01"},
        {i: 3, d: "A blue car.", dk: "2024-01-01"}
    ];
    assert.deepStrictEqual(items(records, "red car"), [2, 1]);
    assert.deepStrictEqual(items(records, "\"red car\""), [2]);
    assert.deepStrictEqual(items(records, "\"car red\""), []);
    // The bonus also counts the small words of the query.
    var beach = [
        {i: 1, d: "The dog ran to the beach.", dk: "2023-01-01"},
        {i: 2, d: "A dog on the beach.", dk: "2019-01-01"}
    ];
    assert.deepStrictEqual(items(beach, "dog on the beach"), [2, 1]);
    var result = search(records, "red car");
    assert.strictEqual(result.sort, "relevance");
});

test("prefix matching while typing", function () {
    var typing = {typing: true};
    var records = [
        {i: 1, t: "Sunset", dk: "2020-01-01"},
        {i: 2, t: "Sundae", dk: "2021-01-01"},
        {i: 3, t: "Lake", dk: "2022-01-01"}
    ];
    assert.deepStrictEqual(items(records, "sun", typing).sort(), [1, 2]);
    assert.deepStrictEqual(items(records, "sun ", typing), []);
    assert.deepStrictEqual(items(records, "sun"), [], "only while typing");
    assert.deepStrictEqual(items(records, "sunse lake", typing), []);
    var both = [{i: 1, t: "Sunset at the lake", dk: "2020-01-01"}];
    assert.deepStrictEqual(items(both, "lake sunse", typing), [1]);
    // A word the records have is that word only, also while typing:
    // "car" is not "cart", "cat" is not "caterpillar".
    var cars = [
        {i: 1, t: "Shopping cart", dk: "2024-01-01"},
        {i: 2, g: ["car"], dk: "2019-01-01"},
        {i: 3, t: "Caterpillar on a leaf", dk: "2024-02-01"},
        {i: 4, t: "Two kittens playing with yarn", g: ["cat"], dk: "2018-01-01"}
    ];
    assert.deepStrictEqual(items(cars, "car", typing), [2]);
    assert.deepStrictEqual(items(cars, "cat", typing), [4]);
    assert.deepStrictEqual(items(cars, "cate", typing), [3]);
    // The answer says how it searched (for the highlights).
    var answer = search(cars, "cat", typing);
    assert.ok(!answer.query.clauses[0].terms[0].prefix);
    assert.ok(search(cars, "cate", typing).query.clauses[0].terms[0].prefix);
    assert.ok(SearchCore.parse("cat", typing).clauses[0].terms[0].prefix, "parse() is untouched");
    var mixed = [
        {i: 1, t: "Lake and sunset", dk: "2024-01-01"},
        {i: 2, t: "Lake", d: "A lake under the morning sun.", dk: "2019-01-01"},
        {i: 3, t: "Lake at sundown", dk: "2023-01-01"}
    ];
    assert.deepStrictEqual(items(mixed, "lake sund", typing), [3]);
    // The start of a longer word scores a quarter of a whole word.
    var partial = [
        {i: 1, t: "Sundown", dk: "2024-01-01"},
        {i: 2, d: "A sundial", dk: "2019-01-01"},
        {i: 3, f: "sundays", dk: "2025-01-01"}
    ];
    // Title 2.5 > description 0.75 > folder 0.25.
    assert.deepStrictEqual(items(partial, "sund", typing), [1, 2, 3]);
});

test("plurals, accents and apostrophes match", function () {
    var records = [
        {i: 1, g: ["cat"], dk: "2020-01-01"},
        {i: 2, d: "A bowl of berries and two boxes.", dk: "2020-01-02"},
        {i: 3, t: "Caf\u00e9 de Flore", dk: "2020-01-03"},
        {i: 4, o: "DON'T WALK", dk: "2020-01-04"},
        {i: 5, t: "Reading glasses", dk: "2020-01-05"},
        {i: 6, d: "Chocolate cookies", dk: "2020-01-06"},
        {i: 7, d: "Three children", dk: "2020-01-07"}
    ];
    assert.deepStrictEqual(items(records, "cats "), [1]);
    assert.deepStrictEqual(items(records, "berry "), [2]);
    assert.deepStrictEqual(items(records, "box "), [2]);
    assert.deepStrictEqual(items(records, "cafe "), [3]);
    assert.deepStrictEqual(items(records, "CAF\u00c9 "), [3]);
    assert.deepStrictEqual(items(records, "dont "), [4]);
    assert.deepStrictEqual(items(records, "don't "), [4]);
    assert.deepStrictEqual(items(records, "glass "), [5]);
    assert.deepStrictEqual(items(records, "cookie "), [6]);
    assert.deepStrictEqual(items(records, "\"chocolate cookie\""), [6]);
    assert.deepStrictEqual(items(records, "child "), [7]);
});

test("exclusions and alternatives", function () {
    var records = [
        {i: 1, g: ["beach", "dog"], dk: "2020-01-01"},
        {i: 2, g: ["beach"], dk: "2020-01-02"},
        {i: 3, g: ["cat"], dk: "2020-01-03"},
        {i: 4, g: ["dog"], dk: "2020-01-04"},
        {i: 5, d: "A red car and a hot dog.", dk: "2020-01-05"}
    ];
    assert.deepStrictEqual(items(records, "beach -dog"), [2]);
    assert.deepStrictEqual(items(records, "-dog"), [3, 2]);
    assert.deepStrictEqual(items(records, "cat OR beach").sort(), [1, 2, 3]);
    assert.deepStrictEqual(items(records, "dog -\"red car\"").sort(), [1, 4]);
    assert.deepStrictEqual(items(records, "cat OR -dog").sort(), [2, 3]);
    // An exclusion never matches as a prefix.
    assert.deepStrictEqual(items(records, "beach -do"), [2, 1]);
});

test("words in one field", function () {
    var records = [
        {i: 1, g: ["beach"], o: "EXIT ONLY", dk: "2020-01-01"},
        {i: 2, d: "A beach with an exit sign.", dk: "2020-01-02"},
        {i: 3, t: "Exit", g: ["ice cream"], dk: "2020-01-03"},
        {i: 4, d: "ice and cream", cm: "Apple iPhone 12", f: "Trips/Rome", n: "IMG_0042.JPG",
         dk: "2020-01-04"}
    ];
    assert.deepStrictEqual(items(records, "tag:beach"), [1]);
    assert.deepStrictEqual(items(records, "text:exit"), [1]);
    assert.deepStrictEqual(items(records, "title:exit"), [3]);
    assert.deepStrictEqual(items(records, "tag:\"ice cream\""), [3]);
    assert.deepStrictEqual(items(records, "\"ice cream\""), [3]);
    assert.deepStrictEqual(items(records, "camera:iphone"), [4]);
    assert.deepStrictEqual(items(records, "folder:rome"), [4]);
    assert.deepStrictEqual(items(records, "file:img_0042"), [4]);
    assert.deepStrictEqual(items(records, "-text:exit exit"), [3, 2]);
});

test("filters by type, people, dates, flags and library", function () {
    var records = [
        {i: 1, l: "lib_a", ty: "photo", pp: "none", dk: "2015-06-01", gp: 1, ds: 1},
        {i: 2, l: "lib_a", ty: "screenshot", dt: "chat", o: "hello", dk: "2019-05-14", ds: 1},
        {i: 3, l: "lib_b", ty: "document", dt: "receipt", o: "TOTAL 12.50", dk: "2019-05-15",
         ds: 1},
        {i: 4, l: "lib_b", ty: "photo", pp: "3-5", dk: "2021-01-01", p: "360", dg: 4, ds: 1},
        {i: 5, l: "lib_b", ty: "photo", pp: "2", dk: "2021-01-02", dg: 4, a: ["dog"],
         qf: ["blurry"], ds: 1},
        {i: 6, l: "lib_a"}
    ];
    var libraries = [{id: "lib_a", name: "Family Photos"}, {id: "lib_b", name: "Phone"}];
    function found(query) {
        return items(records, query, {libraries: libraries}).sort();
    }
    assert.deepStrictEqual(found("type:screenshot"), [2]);
    assert.deepStrictEqual(found("type:receipt"), [3]);
    assert.deepStrictEqual(found("type:document"), [3]);
    assert.deepStrictEqual(found("-type:photo"), [2, 3, 6]);
    assert.deepStrictEqual(found("type:screenshot OR type:receipt"), [2, 3]);
    assert.deepStrictEqual(found("people:none"), [1]);
    assert.deepStrictEqual(found("people:any"), [4, 5]);
    assert.deepStrictEqual(found("people:4"), [4]);
    assert.deepStrictEqual(found("year:2019"), [2, 3]);
    assert.deepStrictEqual(found("year:2015-2019"), [1, 2, 3]);
    assert.deepStrictEqual(found("before:2019-05-15"), [1, 2]);
    assert.deepStrictEqual(found("before:2019"), [1]);
    assert.deepStrictEqual(found("after:2019-05"), [4, 5]);
    assert.deepStrictEqual(found("after:2019-05-14"), [3, 4, 5]);
    assert.deepStrictEqual(found("has:text"), [2, 3]);
    assert.deepStrictEqual(found("has:gps"), [1]);
    assert.deepStrictEqual(found("has:people"), [4, 5]);
    assert.deepStrictEqual(found("has:animals"), [5]);
    assert.deepStrictEqual(found("is:360"), [4]);
    assert.deepStrictEqual(found("is:duplicate"), [4, 5]);
    assert.deepStrictEqual(found("is:undescribed"), [6]);
    assert.deepStrictEqual(found("is:blurry"), [5]);
    assert.deepStrictEqual(found("lib:phone"), [3, 4, 5]);
    assert.deepStrictEqual(found("lib:lib_a"), [1, 2, 6]);
    assert.deepStrictEqual(found("lib:\"family photos\""), [1, 2, 6]);
    assert.deepStrictEqual(found("lib:fam"), [1, 2, 6]);
    var unknown = search(records, "lib:nowhere", {libraries: libraries});
    assert.deepStrictEqual(unknown.items, []);
    assert.strictEqual(unknown.warnings.length, 1);
    var invalid = search(records, "type:selfie");
    assert.deepStrictEqual(invalid.items, []);
    assert.strictEqual(invalid.warnings.length, 1);
    // Only filters: newest first, not by relevance.
    assert.strictEqual(search(records, "year:2019").sort, "newest");
    assert.deepStrictEqual(items(records, "year:2019"), [3, 2]);
});

test("Chinese and Japanese words are found character by character", function () {
    var records = [
        {i: 1, t: "\u6771\u4eac\u30bf\u30ef\u30fc", dk: "2020-01-01"},
        {i: 2, t: "\u4eac\u90fd", dk: "2020-01-02"}
    ];
    assert.deepStrictEqual(items(records, "\u6771\u4eac"), [1]);
    assert.deepStrictEqual(items(records, "\u4eac\u6771"), []);
    assert.deepStrictEqual(items(records, "\u4eac").sort(), [1, 2]);
});

test("equal scores: the newest first", function () {
    var records = [
        {i: 1, t: "Dog", dk: "2019-01-01"},
        {i: 2, t: "Dog", dk: "2021-01-01"},
        {i: 3, t: "Dog"},
        {i: 4, t: "Dog", dk: "2021-01-01", ts: "2021-01-01T08:00:00"}
    ];
    assert.deepStrictEqual(items(records, "dog"), [4, 2, 1, 3]);
});

test("sort orders", function () {
    var records = [
        {i: 1, n: "IMG_10.jpg", dk: "2020-01-01", q: 3, sh: 4, t: "dog"},
        {i: 2, n: "IMG_2.jpg", dk: "2021-01-01", q: 5, sh: 1, t: "dog"},
        {i: 3, n: "a.jpg", q: 5, sh: 5, t: "dog"},
        {i: 4, n: "B.jpg", dk: "2019-05-05", ts: "2019-05-05T10:00:00", t: "dog"},
        {i: 5, t: "Title only", dk: "2018-01-01"}
    ];
    assert.deepStrictEqual(items(records, ""), [2, 1, 4, 5, 3]);
    assert.strictEqual(search(records, "").sort, "newest");
    assert.deepStrictEqual(items(records, "", {sort: "oldest"}), [5, 4, 1, 2, 3]);
    assert.deepStrictEqual(items(records, "", {sort: "name"}), [3, 4, 2, 1, 5]);
    assert.deepStrictEqual(items(records, "", {sort: "quality"}), [3, 2, 1, 4, 5]);
    assert.deepStrictEqual(items(records, "dog", {sort: "oldest"}), [4, 1, 2, 3]);
    assert.deepStrictEqual(items(records, "", {sort: "nonsense"}), [2, 1, 4, 5, 3]);
});

test("hidden, sensitive, private, junk and library options", function () {
    var records = [
        {i: 1, l: "lib_a", t: "beach", dk: "2020-01-01"},
        {i: 2, l: "lib_a", t: "beach", dk: "2020-01-02", h: 1},
        {i: 3, l: "lib_b", t: "beach", dk: "2020-01-03", se: "explicit"},
        {i: 4, l: "lib_b", t: "beach", dk: "2020-01-04", se: "suggestive"},
        {i: 5, l: "lib_b", t: "beach", dk: "2020-01-05", pr: 1},
        {i: 6, l: "lib_b", t: "beach", dk: "2020-01-06", qf: ["accidental"]}
    ];
    assert.deepStrictEqual(items(records, "beach"), [6, 5, 4, 1]);
    assert.deepStrictEqual(items(records, "beach", {hidden: true}), [6, 5, 4, 2, 1]);
    assert.deepStrictEqual(items(records, "is:hidden"), [2]);
    assert.deepStrictEqual(items(records, "beach", {sensitive: true}), [6, 5, 4, 3, 1]);
    assert.deepStrictEqual(items(records, "beach", {sensitive: "strict"}), [6, 5, 1]);
    assert.deepStrictEqual(items(records, "beach", {"private": false}), [6, 4, 1]);
    assert.deepStrictEqual(items(records, "beach", {junk: true}), [5, 4, 1]);
    assert.deepStrictEqual(items(records, "beach", {library: "lib_a"}), [1]);
    assert.deepStrictEqual(items(records, "beach", {library: ["lib_a", "lib_b"]}),
                           [6, 5, 4, 1]);
    assert.deepStrictEqual(items(records, "beach", {library: "all"}), [6, 5, 4, 1]);
    // Hiding a picture in place is seen at once.
    records[0].h = 1;
    assert.deepStrictEqual(items(records, "beach"), [6, 5, 4]);
    records[0].h = 0;
});

test("a prepared index and the per-array cache", function () {
    var records = [{i: 1, t: "one", dk: "2020-01-01"}];
    var index = SearchCore.prepare(records);
    assert.deepStrictEqual(SearchCore.search(index, "one").items, [1]);
    assert.deepStrictEqual(index.search("one").items, [1]);
    assert.strictEqual(index.get(1), records[0]);
    assert.strictEqual(index.get(99), null);
    records.push({i: 2, t: "one two", dk: "2020-01-02"});
    assert.deepStrictEqual(items(records, "one"), [2, 1]);
    assert.deepStrictEqual(SearchCore.search([], "one").items, []);
    assert.deepStrictEqual(SearchCore.search(null, "one").items, []);
    assert.deepStrictEqual(SearchCore.search([null, 5, {i: 3, t: "one"}], "one").items, [3]);
});

/* ---------- Facets --------------------------------------------------------- */

var FACET_RECORDS = [
    {i: 1, l: "lib_a", ty: "photo", pp: "none", dk: "2019-03-01", g: ["beach", "sea"], gp: 1},
    {i: 2, l: "lib_a", ty: "photo", pp: "2", dk: "2019-07-01", g: ["beach"]},
    {i: 3, l: "lib_b", ty: "screenshot", dt: "chat", dk: "2020-01-05", g: ["phone"],
     o: "hi there"},
    {i: 4, l: "lib_b", ty: "screenshot", dk: "2020-02-01", g: ["phone", "Beach"], o: "menu"},
    {i: 5, l: "lib_b", ty: "document", dt: "receipt", dk: "2021-01-01", g: ["receipt"],
     o: "total"},
    {i: 6, l: "lib_b", ty: "photo", pp: "crowd", dk: "2021-08-08", g: ["concert"], p: "360",
     cm: "Camera One", st: "outdoor", td: "night", ev: "concert", s: ["Stage"]}
];

test("facet counts without selections", function () {
    var result = search(FACET_RECORDS, "");
    assert.strictEqual(result.total, 6);
    assert.deepStrictEqual(values(result, "type"),
                           ["photo", "screenshot", "document", "doc:receipt", "doc:chat"]);
    assert.deepStrictEqual(counts(result, "type"),
                           {photo: 3, screenshot: 2, document: 1, "doc:chat": 1,
                            "doc:receipt": 1});
    assert.deepStrictEqual(values(result, "people"), ["none", "2", "crowd"]);
    assert.deepStrictEqual(values(result, "year"), ["2021", "2020", "2019"]);
    assert.deepStrictEqual(counts(result, "year"), {2019: 2, 2020: 2, 2021: 2});
    assert.deepStrictEqual(result.facets.month, []);
    assert.deepStrictEqual(counts(result, "has"), {text: 3, gps: 1, 360: 1});
    assert.deepStrictEqual(counts(result, "tags"),
                           {beach: 3, phone: 2, sea: 1, receipt: 1, concert: 1});
    assert.deepStrictEqual(values(result, "tags")[0], "beach");
    assert.deepStrictEqual(counts(result, "library"), {lib_a: 2, lib_b: 4});
    assert.deepStrictEqual(counts(result, "camera"), {"Camera One": 1});
    assert.deepStrictEqual(counts(result, "subjects"), {stage: 1});
    assert.deepStrictEqual(counts(result, "setting"), {outdoor: 1});
    assert.deepStrictEqual(counts(result, "time"), {night: 1});
    assert.deepStrictEqual(counts(result, "event"), {concert: 1});
    SearchCore.FACETS.forEach(function (facet) {
        assert.ok(Array.isArray(result.facets[facet.key]), facet.key);
    });
});

test("facets are disjunctive: a group's own selection does not change its counts", function () {
    var one = search(FACET_RECORDS, "", {filters: {type: ["screenshot"]}});
    assert.strictEqual(one.total, 2);
    assert.deepStrictEqual(one.items, [4, 3]);
    assert.deepStrictEqual(counts(one, "type"),
                           {photo: 3, screenshot: 2, document: 1, "doc:chat": 1,
                            "doc:receipt": 1});
    assert.ok(one.facets.type[1].selected && !one.facets.type[0].selected);
    assert.deepStrictEqual(counts(one, "year"), {2020: 2});
    assert.deepStrictEqual(counts(one, "tags"), {phone: 2, beach: 1});

    var two = search(FACET_RECORDS, "", {filters: {type: ["screenshot", "photo"]}});
    assert.strictEqual(two.total, 5);
    assert.deepStrictEqual(counts(two, "year"), {2019: 2, 2020: 2, 2021: 1});

    var both = search(FACET_RECORDS, "", {filters: {type: "photo", year: ["2019"]}});
    assert.deepStrictEqual(both.items, [2, 1]);
    assert.deepStrictEqual(counts(both, "type"), {photo: 2});
    assert.deepStrictEqual(counts(both, "year"), {2019: 2, 2021: 1});
    assert.deepStrictEqual(counts(both, "month"), {"2019-03": 1, "2019-07": 1});
    assert.deepStrictEqual(values(both, "month"), ["2019-03", "2019-07"]);

    var month = search(FACET_RECORDS, "", {filters: {year: ["2019"], month: ["2019-07"]}});
    assert.deepStrictEqual(month.items, [2]);
    assert.deepStrictEqual(counts(month, "month"), {"2019-03": 1, "2019-07": 1});
    assert.deepStrictEqual(counts(month, "year"), {2019: 1});

    // Facets count over the matches of the query.
    var query = search(FACET_RECORDS, "beach", {filters: {type: ["photo"]}});
    assert.deepStrictEqual(query.items, [2, 1]);
    assert.deepStrictEqual(counts(query, "type"), {photo: 2, screenshot: 1});

    // Tags are matched without regard to case.
    var tag = search(FACET_RECORDS, "", {filters: {tags: ["BEACH"]}});
    assert.deepStrictEqual(tag.items, [4, 2, 1]);
    var library = search(FACET_RECORDS, "", {filters: {library: ["lib_a"]}});
    assert.deepStrictEqual(library.items, [2, 1]);
    assert.deepStrictEqual(counts(library, "library"), {lib_a: 2, lib_b: 4});
});

test("has values must all match", function () {
    var text = search(FACET_RECORDS, "", {filters: {has: ["text"]}});
    assert.deepStrictEqual(text.items, [5, 4, 3]);
    assert.deepStrictEqual(counts(text, "has"), {text: 3});
    assert.deepStrictEqual(counts(text, "type"),
                           {screenshot: 2, document: 1, "doc:chat": 1, "doc:receipt": 1});
    var both = search(FACET_RECORDS, "", {filters: {has: ["text", "gps"]}});
    assert.strictEqual(both.total, 0);
    assert.deepStrictEqual(counts(both, "has"), {text: 0, gps: 0});
    var gps = search(FACET_RECORDS, "", {filters: {has: ["gps"]}});
    assert.deepStrictEqual(counts(gps, "has"), {gps: 1});
});

test("top values only, but selected values are always listed", function () {
    var records = [];
    for (var k = 0; k < 30; k++) {
        var tags = [];
        for (var t = 0; t <= k; t++) {
            tags.push("tag" + (t < 10 ? "0" : "") + t);
        }
        records.push({i: k + 1, g: tags, dk: "2020-01-01"});
    }
    var plain = search(records, "");
    assert.strictEqual(plain.facets.tags.length, 20);
    assert.strictEqual(plain.facets.tags[0].value, "tag00");
    assert.strictEqual(plain.facets.tags[0].count, 30);
    var chosen = search(records, "", {filters: {tags: ["tag29", "nothing"]}});
    assert.strictEqual(chosen.total, 1);
    var chosenValues = values(chosen, "tags");
    assert.strictEqual(chosen.facets.tags.length, 22);
    assert.ok(chosenValues.indexOf("tag29") >= 0);
    assert.ok(chosenValues.indexOf("nothing") >= 0);
});

test("facet labels", function () {
    assert.strictEqual(SearchCore.facetLabel("type", "doc:receipt"), "Receipts");
    assert.strictEqual(SearchCore.facetLabel("type", "screenshot"), "Screenshots");
    assert.strictEqual(SearchCore.facetLabel("month", "2019-05"), "May 2019");
    assert.strictEqual(SearchCore.facetLabel("people", "3-5"), "3-5 people");
    assert.strictEqual(SearchCore.facetLabel("time", "sunrise_sunset"), "Sunrise or sunset");
    assert.strictEqual(SearchCore.facetLabel("event", "graduation"), "Graduation");
    assert.strictEqual(SearchCore.facetLabel("has", "360"), "360\u00b0 photos");
    assert.strictEqual(SearchCore.facetLabel("tags", "beach"), "beach");
    assert.ok(SearchCore.SYNTAX.length > 5);
    assert.ok(SearchCore.SORTS.indexOf("relevance") >= 0);
});

/* ---------- Duplicate groups ----------------------------------------------- */

var GROUP_RECORDS = [
    {i: 10, dk: "2020-01-03", dg: 12, t: "snow"},
    {i: 11, dk: "2020-01-04", t: "snow"},
    {i: 12, dk: "2020-01-02", dg: 12, t: "other"},
    {i: 13, dk: "2020-01-05", dg: 12, t: "snow"},
    {i: 14, dk: "2020-01-01", dg: 20, t: "snow"},
    {i: 20, dk: "2019-12-31", dg: 20, t: "snow"}
];

test("grouping keeps the order of the results", function () {
    var flat = search(GROUP_RECORDS, "");
    assert.deepStrictEqual(flat.items, [13, 11, 10, 12, 14, 20]);
    assert.deepStrictEqual(flat.groups, {});
    assert.strictEqual(flat.collapsed, 0);
    var grouped = search(GROUP_RECORDS, "", {group: true});
    assert.strictEqual(grouped.total, 6);
    assert.strictEqual(grouped.sort, "newest");
    // Sorted by date, each group is shown as its cover, at the cover's
    // date (12 is from January 2nd, after 11 from the 4th).
    assert.deepStrictEqual(grouped.items, [11, 12, 20]);
    assert.deepStrictEqual(grouped.groups, {12: [12, 13, 10], 20: [20, 14]});
    assert.strictEqual(grouped.collapsed, 3);
    var oldest = search(GROUP_RECORDS, "", {group: true, sort: "oldest"});
    assert.deepStrictEqual(oldest.items, [20, 12, 11]);
    // Other orders: where its first match was, shown as its cover.
    var byQuality = search(GROUP_RECORDS, "", {group: true, sort: "quality"});
    assert.deepStrictEqual(byQuality.items, [12, 11, 20]);
});

test("a group whose cover does not match shows its best match", function () {
    var grouped = search(GROUP_RECORDS, "snow", {group: true});
    assert.deepStrictEqual(grouped.items, [13, 11, 20]);
    assert.deepStrictEqual(grouped.groups, {13: [13, 10], 20: [20, 14]});
    assert.strictEqual(grouped.collapsed, 2);
    // One match of a group is no stack.
    var single = search(GROUP_RECORDS, "snow -text:x", {group: true, filters: {year: ["2019"]}});
    assert.deepStrictEqual(single.items, [20]);
    assert.deepStrictEqual(single.groups, {});
});

test("grouping follows the relevance order", function () {
    var records = [
        {i: 1, d: "lake", dk: "2024-01-01", dg: 2},
        {i: 2, d: "lake", dk: "2023-01-01", dg: 2},
        {i: 3, t: "lake", dk: "2010-01-01", dg: 4},
        {i: 4, d: "lake", dk: "2011-01-01", dg: 4},
        {i: 5, g: ["lake"], dk: "2012-01-01"}
    ];
    assert.deepStrictEqual(items(records, "lake"), [3, 5, 1, 2, 4]);
    var grouped = search(records, "lake", {group: true});
    assert.deepStrictEqual(grouped.items, [4, 5, 2]);
    assert.deepStrictEqual(grouped.groups, {4: [4, 3], 2: [2, 1]});
});

/* ---------- Highlights ----------------------------------------------------- */

var SENTENCE = "The Caf\u00e9's red cars, and a red car.";

test("highlights of words, plurals and phrases", function () {
    assert.deepStrictEqual(SearchCore.highlights(SENTENCE, "red car"),
                           [[11, 14], [15, 19], [27, 30], [31, 34]]);
    assert.deepStrictEqual(SearchCore.highlights(SENTENCE, "\"red car\""),
                           [[11, 19], [27, 34]]);
    assert.deepStrictEqual(SearchCore.highlights(SENTENCE, "cafe"), [[4, 10]]);
    var typing = {typing: true};
    assert.deepStrictEqual(SearchCore.highlights(SENTENCE, SearchCore.parse("ca", typing)),
                           [[4, 10], [15, 19], [31, 34]]);
    assert.deepStrictEqual(SearchCore.highlights(SENTENCE, SearchCore.parse("ca ", typing)), []);
    assert.deepStrictEqual(SearchCore.highlights(SENTENCE, "ca"), []);
    // Small words that were skipped, exclusions and filters mark nothing.
    assert.deepStrictEqual(SearchCore.highlights(SENTENCE, "the red -car type:photo"),
                           [[11, 14], [27, 30]]);
    assert.deepStrictEqual(SearchCore.highlights(SENTENCE, "red OR blue"), [[11, 14], [27, 30]]);
});

test("highlights keep string offsets and respect fields", function () {
    var text = "\ud83d\udc36 dog DOG d\u00f6g";
    assert.deepStrictEqual(SearchCore.highlights(text, "dog"), [[3, 6], [7, 10], [11, 14]]);
    assert.deepStrictEqual(SearchCore.highlights("A beach at sunset", "tag:beach sunset", "d"),
                           [[11, 17]]);
    assert.deepStrictEqual(SearchCore.highlights("A beach at sunset", "tag:beach sunset", "g"),
                           [[2, 7], [11, 17]]);
    assert.deepStrictEqual(SearchCore.highlights("EXIT", "text:exit", "o"), [[0, 4]]);
    assert.deepStrictEqual(SearchCore.highlights("aaa", SearchCore.parse("a", {typing: true})),
                           [[0, 3]]);
    assert.deepStrictEqual(SearchCore.highlights("", "red"), []);
    assert.deepStrictEqual(SearchCore.highlights(null, "red"), []);
    assert.deepStrictEqual(SearchCore.highlights("red", ""), []);
    assert.deepStrictEqual(SearchCore.highlights("'red'", "red"), [[1, 4]]);
    assert.deepStrictEqual(SearchCore.highlights("\u6771\u4eac\u30bf\u30ef\u30fc", "\u6771\u4eac"),
                           [[0, 2]]);
    var parsed = SearchCore.parse("red");
    assert.deepStrictEqual(SearchCore.highlights("red", parsed), [[0, 3]]);
});

/* ---------- More like this and did you mean -------------------------------- */

test("similar: the duplicate group first, then shared tags, then time", function () {
    var records = [
        {i: 1, dg: 1, g: ["beach", "sea"], s: ["Sand"], sc: "beach", ts: "2020-01-01T10:00:00",
         dk: "2020-01-01", q: 5},
        {i: 2, dg: 1, g: ["beach", "sea"], sc: "beach", ts: "2020-01-01T10:00:05",
         dk: "2020-01-01"},
        {i: 3, g: ["beaches", "sea", "palm"], s: ["sand"], sc: "beach", ts: "2021-05-05T09:00:00",
         dk: "2021-05-05"},
        {i: 4, g: ["cake"], ts: "2020-01-01T11:00:00", dk: "2020-01-01"},
        {i: 5, g: ["car"], ts: "2015-03-03T03:00:00", dk: "2015-03-03"},
        {i: 6, g: ["beach", "sea"], s: ["sand"], h: 1},
        {i: 7, g: ["sea"], ts: "2019-01-01T00:00:00", dk: "2019-01-01"}
    ];
    assert.deepStrictEqual(SearchCore.similar(records, 1, 8), [2, 3, 4, 7]);
    assert.deepStrictEqual(SearchCore.similar(records, 1, 2), [2, 3]);
    assert.deepStrictEqual(SearchCore.similar(records, 1, 8, {hidden: true}), [2, 6, 3, 4, 7]);
    // From a member, the cover comes first.
    assert.deepStrictEqual(SearchCore.similar(records, 2, 1), [1]);
    assert.deepStrictEqual(SearchCore.similar(records, 999, 8), []);
    assert.deepStrictEqual(SearchCore.similar(records, 5, 8), []);
    // Without times (an export), the same day counts.
    var exported = [
        {i: 1, g: ["a"], dk: "2020-01-01"},
        {i: 2, g: ["b"], dk: "2020-01-01"},
        {i: 3, g: ["c"], dk: "2020-01-02"}
    ];
    assert.deepStrictEqual(SearchCore.similar(exported, 1), [2]);
});

test("similar: one picture per other duplicate group", function () {
    var records = [
        {i: 1, g: ["dog", "couch", "pet"], sc: "living room", dk: "2020-02-11"},
        // A burst of four beach shots (cover 3), all sharing the dog tags.
        {i: 2, g: ["dog", "pet", "beach"], dg: 3, dk: "2021-07-04"},
        {i: 3, g: ["dog", "pet", "beach"], dg: 3, dk: "2021-07-04"},
        {i: 4, g: ["dog", "pet", "beach"], dg: 3, dk: "2021-07-04"},
        {i: 5, g: ["dog", "pet", "beach", "sand"], dg: 3, dk: "2021-07-04"},
        {i: 6, g: ["dog", "ball"], dk: "2022-08-10"}
    ];
    assert.deepStrictEqual(SearchCore.similar(records, 1, 8), [3, 6]);
});

test("did you mean", function () {
    var records = [
        {i: 1, g: ["beach", "sunset"], d: "Waves on the sand", dk: "2020-01-01"},
        {i: 2, g: ["mountain"], d: "Mountains and beaches", dk: "2020-01-02"}
    ];
    assert.deepStrictEqual(SearchCore.suggest(records, "beahc"),
                           {text: "beach", changes: [{from: "beahc", to: "beach"}]});
    assert.strictEqual(SearchCore.suggest(records, "beach mountian").text, "beach mountain");
    assert.strictEqual(SearchCore.suggest(records, "tag:sunest").text, "tag:sunset");
    assert.strictEqual(SearchCore.suggest(records, "beach"), null);
    // While typing, a start of a word is not a mistake.
    assert.strictEqual(SearchCore.suggest(records, "sunse", {typing: true}), null);
    assert.strictEqual(SearchCore.suggest(records, "sunse").text, "sunset");
    // A correction that finds nothing either is not offered.
    assert.strictEqual(SearchCore.suggest(records, "sunset mountian"), null);
    assert.strictEqual(SearchCore.suggest(records, "beahc", {library: "lib_x"}), null);
    assert.strictEqual(SearchCore.suggest(records, "xyzzyq"), null);
    assert.strictEqual(SearchCore.suggest(records, ""), null);
});

/* ---------- Many records --------------------------------------------------- */

var WORDS = ("beach sunset dog cat car tree mountain lake river city street house garden " +
    "flower bird boat bridge snow forest park building window door table chair cake coffee " +
    "pizza sky cloud rain night light child family friend wedding birthday party concert " +
    "museum train plane bicycle phone screen receipt menu sign book paper road field farm " +
    "horse sheep fish shell sand wave rock cliff island harbor market shop church castle " +
    "tower statue fountain lamp candle fire ice glass bottle cup plate bowl kitchen bedroom " +
    "office desk computer keyboard laptop camera painting drawing poster ticket card letter " +
    "map chart red blue green yellow orange purple white black old young happy smiling " +
    "running sitting standing walking large small bright dark").split(" ");
var CAMERAS = ["Apple iPhone 12", "Apple iPhone 8", "Google Pixel 6", "Canon EOS 80D",
               "Nikon D750", "Sony A7 III", "Samsung Galaxy S21", "Fujifilm X-T3"];
var TYPES = ["photo", "photo", "photo", "photo", "photo", "photo", "photo", "screenshot",
             "document", "artwork"];
var DOCS = ["receipt", "chat", "web", "menu", "sign", "letter"];
var PEOPLE = ["none", "none", "1", "2", "3-5", "6-10", "crowd"];
var SETTINGS = ["indoor", "outdoor", "outdoor", "vehicle", "unknown"];
var EVENTS = ["none", "none", "none", "birthday", "wedding", "travel", "party", "meal"];

function makeRecords(count, seed) {
    var random = randomSource(seed);
    function pick(list) {
        return list[Math.floor(random() * list.length)];
    }
    function words(min, max) {
        var out = [];
        var length = min + Math.floor(random() * (max - min + 1));
        for (var k = 0; k < length; k++) {
            out.push(pick(WORDS));
        }
        return out;
    }
    // Rare made-up words, so the word list is as long as a real library's.
    function rareWord() {
        var letters = "abcdefghijklmnopqrstuvwxyz";
        var length = 3 + Math.floor(random() * 7);
        var out = "";
        for (var k = 0; k < length; k++) {
            out += letters.charAt(Math.floor(random() * 26));
        }
        return out;
    }
    var records = [];
    var group = 0;
    for (var i = 1; i <= count; i++) {
        var year = 2005 + Math.floor(random() * 20);
        var month = 1 + Math.floor(random() * 12);
        var day = 1 + Math.floor(random() * 28);
        var date = year + "-" + (month < 10 ? "0" : "") + month + "-" + (day < 10 ? "0" : "") + day;
        var hour = Math.floor(random() * 24);
        var rec = {
            i: i, l: random() < 0.7 ? "lib_a" : "lib_b",
            n: "IMG_" + (1000 + i) + ".jpg", f: year + "/" + pick(WORDS),
            t: words(3, 6).join(" "), d: words(20, 40).join(" ") + ".",
            g: words(8, 12), s: words(3, 6), ty: pick(TYPES), dk: date,
            ts: date + "T" + (hour < 10 ? "0" : "") + hour + ":30:00", cm: pick(CAMERAS),
            q: 1 + Math.floor(random() * 5), sh: 1 + Math.floor(random() * 5),
            w: 4032, hgt: 3024, ds: 1
        };
        if (rec.ty !== "photo" && random() < 0.6) {
            rec.dt = pick(DOCS);
        }
        if (random() < 0.85) {
            rec.pp = pick(PEOPLE);
            rec.st = pick(SETTINGS);
            rec.ev = pick(EVENTS);
            rec.c = words(1, 3);
        }
        if (random() < 0.2) {
            var text = [];
            for (var k = 0; k < 15; k++) {
                text.push(random() < 0.5 ? rareWord() : pick(WORDS));
            }
            rec.o = text.join(" ");
        }
        if (random() < 0.3) {
            rec.g.push(rareWord());
        }
        if (random() < 0.2) {
            rec.a = [pick(["dog", "cat", "bird", "horse"])];
        }
        if (random() < 0.5) {
            rec.gp = 1;
        }
        if (random() < 0.01) {
            rec.p = "360";
        }
        if (random() < 0.05) {
            rec.qf = ["blurry"];
        }
        if (random() < 0.08 && i > 3) {
            group = i - 1 - Math.floor(random() * 3);
            rec.dg = group;
            records[group - 1].dg = group;
        }
        if (random() < 0.02) {
            rec.h = 1;
        }
        records.push(rec);
    }
    return records;
}

test("matches agree with a plain scan of every record", function () {
    var records = makeRecords(2000, 11);
    var index = SearchCore.prepare(records);
    function plainTag(word, prefix) {
        var wanted = SearchCore.forms(word);
        return records.filter(function (rec) {
            return !rec.h && rec.g.some(function (tag) {
                return SearchCore.tokens(tag).some(function (token) {
                    if (prefix && token.indexOf(word) === 0) {
                        return true;
                    }
                    return SearchCore.forms(token).some(function (form) {
                        return wanted.indexOf(form) >= 0;
                    });
                });
            });
        }).map(function (rec) {
            return rec.i;
        }).sort(function (a, b) {
            return a - b;
        });
    }
    ["beach", "cats", "glasses", "boxes", "sand"].forEach(function (word) {
        var found = index.search("tag:" + word + " ").items.sort(function (a, b) {
            return a - b;
        });
        assert.deepStrictEqual(found, plainTag(word, false), word);
    });
    // While typing: the start of longer words, unless it is a word itself
    // ("x" is one: a camera is called X-T3).
    assert.ok(index.knowsWord("x", false));
    ["b", "be", "ca", "s", "x"].forEach(function (word) {
        var found = index.search("tag:" + word, {typing: true}).items.sort(function (a, b) {
            return a - b;
        });
        assert.deepStrictEqual(found, plainTag(word, !index.knowsWord(word, false)), word);
    });
    // Every grouped item's stack holds members of one group, in order.
    var grouped = index.search("beach", {group: true});
    var flat = index.search("beach");
    var seen = [];
    grouped.items.forEach(function (id) {
        (grouped.groups[id] || [id]).forEach(function (member) {
            seen.push(member);
        });
    });
    assert.strictEqual(seen.length, flat.total);
    assert.strictEqual(grouped.total - grouped.collapsed, grouped.items.length);
});

test("10k records: every query under 50 ms", function () {
    var records = makeRecords(10000, 3);
    var started = now();
    var index = SearchCore.prepare(records);
    var built = now() - started;
    var queries = ["", "beach", "b", "bea", "sunset beach", "\"red car\"", "beach -dog",
                   "cat OR dog", "tag:beach year:2015-2019", "type:screenshot", "has:text receipt",
                   "people:none outdoor", "dog on the beach", "s", "x", "zzzzzz",
                   "camera:iphone after:2019", "is:duplicate", "red blue green yellow"];
    var typing = "sunset by the lake";
    for (var k = 1; k <= typing.length; k++) {
        queries.push(typing.slice(0, k));
    }
    var optionSets = [
        {},
        {group: true},
        {filters: {type: ["photo", "screenshot"], tags: ["beach"], has: ["gps"]}},
        {sort: "name", junk: true, library: "lib_a"},
        {group: true, filters: {year: ["2019"]}, sort: "quality"}
    ];
    var worst = 0;
    var worstQuery = "";
    var times = [];
    queries.forEach(function (query) {
        optionSets.forEach(function (options) {
            options.typing = true;                  // the slowest way (prefixes)
            index.search(query, options);           // warm up
            var runs = [];
            for (var run = 0; run < 3; run++) {
                var start = now();
                var result = index.search(query, options);
                runs.push(now() - start);
                assert.ok(result.total >= 0);
            }
            var middle = median(runs);
            times.push(middle);
            if (middle > worst) {
                worst = middle;
                worstQuery = JSON.stringify(query) + " " + JSON.stringify(options);
            }
        });
    });
    var start = now();
    var similar = index.similar(500, 8);
    var similarFirst = now() - start;           // also works out what it compares
    assert.ok(similar.length > 0);
    start = now();
    index.similar(1500, 8);
    var similarTime = now() - start;
    console.log("    index built in " + built.toFixed(0) + " ms; " + times.length +
                " searches, median " + median(times).toFixed(1) + " ms, slowest " +
                worst.toFixed(1) + " ms (" + worstQuery + "); similar " +
                similarFirst.toFixed(1) + " ms the first time, then " +
                similarTime.toFixed(1) + " ms");
    assert.ok(worst < 50, "slowest search took " + worst.toFixed(1) + " ms: " + worstQuery);
    assert.ok(similarTime < 50, "similar took " + similarTime.toFixed(1) + " ms");
    assert.ok(similarFirst < 500, "the first similar took " + similarFirst.toFixed(1) + " ms");
    assert.ok(built < 10000, "building the index took " + built.toFixed(0) + " ms");
    var all = index.search("");
    assert.strictEqual(all.total, records.filter(function (rec) {
        return !rec.h;
    }).length);
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
