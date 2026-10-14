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

test("an export's pictures of one day sort in the order they were taken (tn)", function () {
    // Exports have no times: tn is the place in capture order within the day.
    var records = [
        {i: "aa", t: "dog", dk: "2020-07-01", tn: 2},
        {i: "zz", t: "dog", dk: "2020-07-01", tn: 1},
        {i: "mm", t: "dog", dk: "2020-07-01", tn: 3},
        {i: "bb", t: "dog", dk: "2020-06-30"},
        {i: "cc", t: "dog"}
    ];
    assert.deepStrictEqual(items(records, ""), ["mm", "aa", "zz", "bb", "cc"]);
    assert.deepStrictEqual(items(records, "", {sort: "oldest"}), ["bb", "zz", "aa", "mm", "cc"]);
    assert.deepStrictEqual(items(records, "dog"), ["mm", "aa", "zz", "bb", "cc"]);
    // Taken one after the other that day: more alike.
    var exported = [
        {i: 1, g: ["a"], dk: "2020-01-01", tn: 1},
        {i: 2, g: ["b"], dk: "2020-01-01", tn: 9},
        {i: 3, g: ["c"], dk: "2020-01-01", tn: 2}
    ];
    assert.deepStrictEqual(SearchCore.similar(exported, 1), [3, 2]);
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
    // (360 videos too.)
    assert.strictEqual(SearchCore.facetLabel("has", "360"), "360\u00b0 photos and videos");
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

/* ---------- Drives ---------------------------------------------------------- */

// Two drives (clips 30-33 and 40-42, keyed by their first clips) and a
// lone video, all made up. 41 is an exact copy of 50 (a duplicate group).
var DRIVE_RECORDS = [
    {i: 30, mv: 1, dr: 60, dk: "2023-08-17", ts: "2023-08-17T08:00:00", dv: 30,
     t: "Leaving the driveway", dvt: "Morning drive to the coast",
     dvd: "A drive along a coastal highway past a lighthouse.", dvg: ["highway", "coast"]},
    {i: 31, mv: 1, dr: 60, dk: "2023-08-17", ts: "2023-08-17T08:01:00", dv: 30, dx: 1,
     t: "Suburban street"},
    {i: 33, mv: 1, dr: 22, dk: "2023-08-17", ts: "2023-08-17T08:03:00", dv: 30, dx: 3,
     dh: 60, t: "A bridge over a river"},
    {i: 32, mv: 1, dr: 60, dk: "2023-08-17", ts: "2023-08-17T08:02:00", dv: 30, dx: 2,
     t: "Freeway traffic"},
    {i: 40, mv: 1, dr: 60, dk: "2023-08-18", ts: "2023-08-18T09:00:00", dv: 40,
     t: "Parking garage"},
    {i: 41, mv: 1, dr: 60, dk: "2023-08-18", ts: "2023-08-18T09:01:00", dv: 40, dx: 1,
     t: "Mountain road", dg: 41},
    {i: 42, mv: 1, dr: 60, dk: "2023-08-18", ts: "2023-08-18T09:02:00", dv: 40, dx: 2,
     t: "Mountain pass in snow"},
    {i: 50, mv: 1, dr: 60, dk: "2023-08-19", ts: "2023-08-19T10:00:00", t: "Mountain road",
     dg: 41},
    {i: 60, dk: "2023-08-16", t: "A bridge at night"}
];

test("drives fold into one result each, shown as their middle clip", function () {
    var flat = search(DRIVE_RECORDS, "");
    assert.strictEqual(flat.items.length, 9);
    // (The drives are listed folded or not: the viewer plays them whole.)
    assert.deepStrictEqual(flat.drives[30].clips, [30, 31, 32, 33]);
    var folded = search(DRIVE_RECORDS, "", {drives: true});
    assert.strictEqual(folded.total, 9);
    // Without words each drive shows as the middle one of its clips (not
    // its first, often the car still parked), where that clip's date is.
    assert.deepStrictEqual(folded.items, [50, 41, 31, 60]);
    assert.deepStrictEqual(folded.drives, {
        30: {clips: [30, 31, 32, 33], matched: [33, 32, 31, 30], shown: 31, text: 30},
        40: {clips: [40, 41, 42], matched: [42, 41, 40], shown: 41, text: null}});
    // (Drives are no duplicate stacks.)
    assert.deepStrictEqual(folded.groups, {});
    assert.strictEqual(folded.collapsed, 0);
    // A clip of a folded drive is never in a duplicate group: 50 stays alone.
    var both = search(DRIVE_RECORDS, "", {drives: true, group: true});
    assert.deepStrictEqual(both.items, [50, 41, 31, 60]);
    assert.strictEqual(both.groups[50], undefined);
    // Without drives the duplicate group is as before (41 its cover).
    var dupes = search(DRIVE_RECORDS, "", {group: true});
    assert.deepStrictEqual(dupes.groups[41], [41, 50]);
    // Oldest first: the drive sits where its shown clip is.
    assert.deepStrictEqual(items(DRIVE_RECORDS, "", {drives: true, sort: "oldest"}),
                           [60, 31, 41, 50]);
});

test("an album's drive is made of its clips; a drive found by its words is whole", function () {
    var albums = [{id: 1, name: "Trip", kind: "manual", items: [31, 60]},
                  {id: 2, name: "Coast", kind: "manual", items: [33, 30, 32]},
                  {id: 3, name: "Lighthouse", kind: "smart", query: "lighthouse", options: {}}];
    // One clip of a drive: that clip alone, as an export of the album has it.
    var trip = search(DRIVE_RECORDS, "album:Trip", {albums: albums, drives: true});
    assert.deepStrictEqual(trip.items, [31, 60]);
    assert.deepStrictEqual(trip.drives, {});
    // Three of its clips: a drive of those three, in the drive's order.
    var coast = search(DRIVE_RECORDS, "album:Coast", {albums: albums, drives: true});
    assert.deepStrictEqual(coast.drives[30].clips, [30, 32, 33]);
    assert.strictEqual(coast.items.length, 1);
    // Elsewhere the drive holds them all.
    assert.deepStrictEqual(search(DRIVE_RECORDS, "bridge", {drives: true}).drives[30].clips,
                           [30, 31, 32, 33]);
    // Found by its own words only: its cover stands for it, or with
    // wholeDrives all of its clips, in its order, where it is...
    assert.deepStrictEqual(items(DRIVE_RECORDS, "lighthouse"), [31]);
    assert.deepStrictEqual(items(DRIVE_RECORDS, "lighthouse", {wholeDrives: true}),
                           [30, 31, 32, 33]);
    // ...but a clip that matches by itself stands for itself.
    assert.deepStrictEqual(items(DRIVE_RECORDS, "bridge", {wholeDrives: true}).sort(),
                           [33, 60]);
    // A smart album holds a drive found by its words whole (the Albums
    // page counts them so, and Copy to a new album keeps them all).
    assert.deepStrictEqual(items(DRIVE_RECORDS, "album:Lighthouse",
                                 {albums: albums, sort: "album", group: false,
                                  wholeDrives: true}), [30, 31, 32, 33]);
});

test("a search hit inside a drive shows the drive, as the clip that matched", function () {
    var index = SearchCore.prepare(DRIVE_RECORDS);
    var result = index.search("bridge", {drives: true});
    // The bridge clip of the first drive, and the photo of a bridge.
    assert.deepStrictEqual(result.items.slice().sort(), [33, 60]);
    assert.deepStrictEqual(result.drives, {30: {clips: [30, 31, 32, 33], matched: [33],
                                                shown: 33, text: 30}});
    assert.deepStrictEqual(index.driveClips(30), [30, 31, 32, 33]);
    assert.deepStrictEqual(index.driveClips("40"), [40, 41, 42]);
    assert.deepStrictEqual(index.driveClips(99), []);
    // Two clips match: the drive shows as the better (here newer) one.
    var mountain = index.search("mountain", {drives: true});
    assert.deepStrictEqual(mountain.items, [50, 42]);
    assert.deepStrictEqual(mountain.drives[40].matched, [42, 41]);
});

test("a drive's own words find it, as its cover, never as the clip that carries them", function () {
    // The words are the drive's: they find its cover (the middle clip),
    // not clip 30 that carries them, whose own words are about leaving.
    var result = search(DRIVE_RECORDS, "lighthouse", {drives: true});
    assert.deepStrictEqual(result.items, [31]);
    assert.deepStrictEqual(result.drives[30].matched, []);   // (none by itself)
    assert.strictEqual(result.drives[30].shown, 31);
    assert.deepStrictEqual(items(DRIVE_RECORDS, "lighthouse"), [31]);
    assert.deepStrictEqual(items(DRIVE_RECORDS, "coast highway"), [31]);
    // A clip that matches another part by its own words passes the drive's
    // part too: leaving, on a drive past a lighthouse.
    assert.deepStrictEqual(items(DRIVE_RECORDS, "leaving lighthouse", {drives: true}), [30]);
    assert.deepStrictEqual(items(DRIVE_RECORDS, "bridge coast"), [33]);
    // A field searches the clips' own words only; -word too.
    assert.deepStrictEqual(items(DRIVE_RECORDS, "title:lighthouse"), []);
    assert.strictEqual(items(DRIVE_RECORDS, "-lighthouse").length, 9);
    assert.deepStrictEqual(items(DRIVE_RECORDS, "is:drive").length, 7);
    assert.deepStrictEqual(items(DRIVE_RECORDS, "is:dashcam", {drives: true}), [41, 31]);
    assert.deepStrictEqual(items(DRIVE_RECORDS, "-is:drive").sort(), [50, 60]);
    // "drive" is a kind word of the clips of drives.
    assert.ok(items(DRIVE_RECORDS, "drive").indexOf(41) >= 0);
    assert.deepStrictEqual(SearchCore.highlights("Morning drive to the coast", "coast",
                                                 "dvt"), [[21, 26]]);
    assert.deepStrictEqual(SearchCore.highlights("Morning drive to the coast", "title:coast",
                                                 "dvt"), []);
});

test("a drive's own words do not make its cover a match when a clip matches by itself", function () {
    var records = DRIVE_RECORDS.map(function (rec) {
        var copy = JSON.parse(JSON.stringify(rec));
        if (copy.i === 30) {
            copy.dvd += " A stop at a gas station.";
        }
        if (copy.i === 32) {
            copy.t = "Gas station by the freeway";
        }
        return copy;
    });
    var result = search(records, "gas station", {drives: true});
    // The clip of the gas station stands for the drive, ringed; the cover
    // (31, by the drive's words only) is no match of its own.
    assert.deepStrictEqual(result.items, [32]);
    assert.strictEqual(result.total, 1);
    assert.deepStrictEqual(result.drives[30].matched, [32]);
    assert.deepStrictEqual(items(records, "gas station"), [32]);
    assert.deepStrictEqual(items(records, "gas station", {sort: "newest", drives: true}), [32]);
});

test("a drive's clips are those the search may show (hidden, sensitive, private, junk)", function () {
    var records = [
        {i: 1, mv: 1, dr: 60, dk: "2023-08-17", ts: "2023-08-17T08:00:00", dv: 1, h: 1,
         t: "Garage"},
        {i: 2, mv: 1, dr: 60, dk: "2023-08-17", ts: "2023-08-17T08:01:00", dv: 1, dx: 1,
         t: "Street", dvt: "Morning drive to the coast", dvd: "Past a lighthouse."},
        {i: 3, mv: 1, dr: 60, dk: "2023-08-17", ts: "2023-08-17T08:02:00", dv: 1, dx: 2,
         t: "Crash scene", se: "graphic"},
        {i: 4, mv: 1, dr: 60, dk: "2023-08-17", ts: "2023-08-17T08:03:00", dv: 1, dx: 3,
         t: "Freeway", pr: 1},
        {i: 5, mv: 1, dr: 60, dk: "2023-08-17", ts: "2023-08-17T08:04:00", dv: 1, dx: 4,
         t: "Blur", qf: ["accidental"]},
        {i: 6, mv: 1, dr: 60, dk: "2023-08-17", ts: "2023-08-17T08:05:00", dv: 1, dx: 5,
         t: "Bridge"}
    ];
    var index = SearchCore.prepare(records);
    var shown = index.search("", {drives: true});
    assert.deepStrictEqual(shown.drives[1].clips, [2, 4, 5, 6]);
    assert.strictEqual(shown.total, 4);
    var strict = index.search("", {drives: true, "private": false, junk: true});
    assert.deepStrictEqual(strict.drives[1].clips, [2, 6]);
    var all = index.search("", {drives: true, hidden: true, sensitive: true});
    assert.deepStrictEqual(all.drives[1].clips, [1, 2, 3, 4, 5, 6]);
    assert.deepStrictEqual(index.driveClips(1, {}), [2, 4, 5, 6]);
    assert.deepStrictEqual(index.driveClips(1, {hidden: true}), [1, 2, 4, 5, 6]);
    // The drive's words find it whatever its clip that carries them.
    assert.deepStrictEqual(index.search("lighthouse", {drives: true, junk: true}).items, [4]);
    // Fewer than two clips to show: no drive.
    var one = index.search("", {drives: true, ids: [6]});
    assert.deepStrictEqual(one.items, [6]);
    assert.deepStrictEqual(one.drives, {});
});

test("a drive's own words follow its flags (dvs, dvp) like a picture's", function () {
    var records = [
        {i: 1, mv: 1, dr: 60, dv: 1, t: "Garage", dvt: "Crash on the road", dvs: "graphic",
         dvp: 1},
        {i: 2, mv: 1, dr: 60, dv: 1, dx: 1, t: "Street"},
        {i: 3, mv: 1, dr: 60, dv: 1, dx: 2, t: "Freeway"}
    ];
    var index = SearchCore.prepare(records);
    assert.deepStrictEqual(index.search("crash", {drives: true}).items, []);
    var shown = index.search("crash", {drives: true, sensitive: true});
    assert.deepStrictEqual(shown.items, [2]);
    assert.strictEqual(shown.drives[1].text, 1);
    assert.deepStrictEqual(index.search("crash", {sensitive: true, "private": false}).items, []);
    // Its clips still show; only its words are not shown.
    var plain = index.search("", {drives: true});
    assert.strictEqual(plain.drives[1].text, null);
    assert.strictEqual(plain.drives[1].clips.length, 3);
});

test("a drive whose first clip is left out shows its middle one", function () {
    var records = DRIVE_RECORDS.filter(function (rec) {
        return rec.i !== 30;
    });
    var result = search(records, "", {drives: true});
    assert.deepStrictEqual(result.items, [50, 41, 32, 60]);
    assert.deepStrictEqual(result.drives[30].clips, [31, 32, 33]);
    assert.deepStrictEqual(SearchCore.prepare(records).driveClips(30), [31, 32, 33]);
});

test("meaning: the clip that carries a drive's words stands for it only when no other is close", function () {
    // Clip 30 carries the drive's words; its vector holds them too.
    function records() {
        return DRIVE_RECORDS.slice(0, 4).map(function (rec) {
            return JSON.parse(JSON.stringify(rec));
        });
    }
    var index = meaningIndex(records(), {30: closeTo(0.95), 32: closeTo(0.9, 2),
                                         31: closeTo(0.1, 3), 33: closeTo(0.1, 4)});
    var result = index.search("seaside", {drives: true, meaning: "meaning", vector: QUERY});
    // 30 and 32 are close; 30 by its drive's words: 32 stands for the drive.
    assert.deepStrictEqual(result.items.slice(0, 1), [32]);
    assert.strictEqual(result.drives[30].shown, 32);
    assert.deepStrictEqual(result.drives[30].matched, [32]);
    // Only the carrier is close: the drive is found, shown as its cover.
    index = meaningIndex(records(), {30: closeTo(0.95), 32: closeTo(0.1, 2),
                                     31: closeTo(0.1, 3), 33: closeTo(0.1, 4)});
    result = index.search("seaside", {drives: true, meaning: "meaning", vector: QUERY});
    assert.strictEqual(result.drives[30].shown, 31);
    assert.deepStrictEqual(result.drives[30].matched, []);
});

test("only the pictures picked on the map (ids)", function () {
    var picked = search(FACET_RECORDS, "", {ids: [2, 4, 6, 99]});
    assert.deepStrictEqual(picked.items, [6, 4, 2]);
    assert.strictEqual(picked.total, 3);
    // Facets count those pictures only.
    assert.deepStrictEqual(counts(picked, "type"), {photo: 2, screenshot: 1});
    // With a query, filters and hiding: all of them still apply.
    assert.deepStrictEqual(items(FACET_RECORDS, "beach", {ids: [1, 2, 3]}), [2, 1]);
    assert.deepStrictEqual(items(FACET_RECORDS, "", {ids: [1, 2, 4], filters: {type: ["photo"]}}),
                           [2, 1]);
    assert.deepStrictEqual(items(FACET_RECORDS, "", {ids: [4, 5], library: "lib_a"}), []);
    // Ids come as numbers or as text; an empty list finds nothing, no list everything.
    assert.deepStrictEqual(items(FACET_RECORDS, "", {ids: ["5", "1"]}), [5, 1]);
    assert.deepStrictEqual(items(FACET_RECORDS, "", {ids: []}), []);
    assert.strictEqual(search(FACET_RECORDS, "", {ids: null}).total, 6);
    // Stacks: the members picked are grouped as usual.
    var grouped = search(GROUP_RECORDS, "", {group: true, ids: [10, 13, 14]});
    assert.deepStrictEqual(grouped.items, [13, 14]);
    assert.deepStrictEqual(grouped.groups, {13: [13, 10]});
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

/* ---------- Synonyms, real things and text-only matches --------------------- */

test("synonyms find other words, below the word itself", function () {
    var records = [
        {i: 1, t: "Children at the park", g: ["child", "park"], dk: "2024-01-01"},
        {i: 2, t: "Kids on the swings", g: ["kid", "swing"], dk: "2019-01-01"},
        {i: 3, t: "Toddler in a sandbox", dk: "2023-01-01"},
        {i: 4, t: "Adults at the park", dk: "2025-01-01"}
    ];
    // The word itself first (although older), then its synonyms.
    assert.deepStrictEqual(items(records, "kids"), [2, 1, 3]);
    assert.deepStrictEqual(items(records, "children"), [1, 2, 3]);
    // A toddler is a kid, but most kids are not toddlers.
    assert.deepStrictEqual(items(records, "toddler"), [3]);
    // Never for quoted words or other fields than tag: and animal:.
    assert.deepStrictEqual(items(records, "\"kids\""), [2]);
    assert.deepStrictEqual(items(records, "title:kids"), [2]);
    assert.deepStrictEqual(items(records, "tag:kids"), [2, 1]);
    // A synonym scores less than the word itself, in the same field.
    var cars = [
        {i: 1, t: "Automobile at the show", dk: "2025-01-01"},
        {i: 2, t: "Car at the show", dk: "2018-01-01"}
    ];
    assert.deepStrictEqual(items(cars, "car"), [2, 1]);
    assert.deepStrictEqual(items(cars, "automobile"), [1, 2]);
    // Excluding a word excludes its synonyms too.
    assert.deepStrictEqual(items(records, "park -kids"), [4]);
    // The synonyms are marked as matches.
    assert.deepStrictEqual(SearchCore.highlights("Children at the park", "kids"), [[0, 8]]);
    assert.deepStrictEqual(SearchCore.highlights("Children at the park", "\"kids\""), []);
});

test("a synonym stays below the word itself in tags, and out of the text in a picture", function () {
    var records = [
        {i: 1, t: "Mother duck with her ducklings", s: ["mother duck"], a: ["duck"],
         dk: "2025-01-01"},
        {i: 2, t: "Woman holding a toddler", g: ["mom", "toddler"], dk: "2010-01-01"},
        {i: 3, t: "Newspaper on a table", o: "MOTHER'S DAY SALE", dk: "2024-01-01"}
    ];
    // The picture tagged "mom" first; a newspaper that reads "mother" is
    // not found at all (the word itself finds it).
    assert.deepStrictEqual(items(records, "mom"), [2, 1]);
    assert.deepStrictEqual(items(records, "mother"), [1, 2, 3]);
});

test("a word with other meanings only finds its synonym one way", function () {
    var records = [
        {i: 1, t: "Autumn leaves in the park", dk: "2025-01-01"},
        {i: 2, t: "Rock fall warning sign", dk: "2024-01-01"},
        {i: 3, t: "Walk in the woods", dk: "2023-01-01"},
        {i: 4, t: "Old wood table", dk: "2022-01-01"},
        {i: 5, t: "Forest trail", dk: "2021-01-01"},
        {i: 6, t: "A gift for grandma", dk: "2020-01-01"},
        {i: 7, t: "Present day skyline", dk: "2019-01-01"}
    ];
    // "fall leaves" are autumn leaves; but "autumn" is no rock fall.
    assert.deepStrictEqual(items(records, "fall"), [2, 1]);
    assert.deepStrictEqual(items(records, "autumn"), [1]);
    // "forest" does not find every wooden table.
    assert.deepStrictEqual(items(records, "forest"), [5]);
    assert.deepStrictEqual(items(records, "woods").slice(0, 2), [3, 4]);
    assert.ok(items(records, "woods").indexOf(5) > 0);
    // A gift is a present; "present" is not always a gift.
    assert.deepStrictEqual(items(records, "gift"), [6]);
    assert.deepStrictEqual(items(records, "present"), [7, 6]);
});

test("facet values of tags and subjects group plurals", function () {
    var records = [
        {i: 1, g: ["dog", "glass"], s: ["Golden Retriever"]},
        {i: 2, g: ["dogs", "glasses"], s: ["golden retrievers"]},
        {i: 3, g: ["Dogs"], s: ["golden retrievers"]},
        {i: 4, g: ["cat"]}
    ];
    var result = search(records, "");
    // One value, shown the way it is written most often.
    assert.deepStrictEqual(counts(result, "tags"),
                           {dogs: 3, glass: 1, glasses: 1, cat: 1});
    assert.deepStrictEqual(counts(result, "subjects"), {"golden retrievers": 3});
    // Either form selects all of them; the entry shows it as selected.
    [["dog"], ["dogs"], ["Dog"]].forEach(function (chosen) {
        var picked = search(records, "", {filters: {tags: chosen}});
        assert.deepStrictEqual(picked.items.slice().sort(), [1, 2, 3], chosen[0]);
        var entry = picked.facets.tags.filter(function (item) {
            return item.selected;
        });
        assert.strictEqual(entry.length, 1);
        assert.strictEqual(entry[0].value, chosen[0].toLowerCase());
        assert.strictEqual(entry[0].count, 3);
    });
    // "glasses" are not glass -- but wine glasses are wine glasses.
    assert.deepStrictEqual(search(records, "", {filters: {tags: ["glasses"]}}).items, [2]);
    var drinks = [{i: 1, s: ["wine glasses"]}, {i: 2, s: ["wine glass"]},
                  {i: 3, s: ["wine glasses"]}, {i: 4, s: ["reading glasses"]},
                  {i: 5, s: ["reading glass"]}];
    assert.deepStrictEqual(counts(search(drinks, ""), "subjects"),
                           {"wine glasses": 3, "reading glasses": 1, "reading glass": 1});
});

test("the real animal, vehicle or food ranks above toys, statues and pictures of it", function () {
    var records = [
        {i: 1, t: "Stuffed toy dog on a bed", s: ["stuffed dog"], g: ["dog", "toy"],
         ty: "photo", dk: "2025-01-01"},
        {i: 2, t: "Brown dog in the yard", a: ["brown dog"], g: ["dog"], ty: "photo",
         dk: "2015-01-01"},
        {i: 3, t: "A dog in watercolor", g: ["dog", "painting"], ty: "artwork",
         dk: "2024-01-01"},
        {i: 4, t: "Bronze dog statue in a park", g: ["dog", "statue"], ty: "photo",
         dk: "2023-01-01"},
        {i: 5, t: "Puppy asleep on a rug", a: ["puppy"], g: ["puppy"], dk: "2014-01-01"},
        {i: 6, d: "A large painting of a dog hangs over the sofa.", dk: "2022-01-01"},
        {i: 7, t: "Dog on the beach", a: ["dog"], dk: "2013-01-01"},
        {i: 8, t: "Dog at the vet", a: ["dog"], dk: "2012-01-01"},
        {i: 9, t: "Hot dog with mustard", fo: ["hot dog"], dk: "2026-01-01"},
        {i: 10, t: "Dog and its rubber toy", a: ["dog"], s: ["rubber toy"], dk: "2011-01-01"}
    ];
    var found = items(records, "dog");
    // The real dogs (a puppy is one too) first.
    assert.deepStrictEqual(found.slice(0, 5).sort(), [10, 2, 5, 7, 8].sort());
    // The toy, the painting, the statue, the dog in a painting and the hot
    // dog (no dog at all) last.
    assert.deepStrictEqual(found.slice(5).sort(), [1, 3, 4, 6, 9]);
    // Unless they are what is asked for: then they come first, even when
    // a real dog (a puppy) plays with a toy.
    records.push({i: 11, t: "Puppy plays with a toy", a: ["puppy"], g: ["puppy", "toy"],
                  dk: "2027-01-01"});
    assert.strictEqual(items(records, "toy dog")[0], 1);
    assert.strictEqual(items(records, "dog statue")[0], 4);
    assert.strictEqual(items(records, "hot dog")[0], 9);
    // Art and documents ask for depictions too.
    var art = [
        {i: 1, t: "Dog at the fair", a: ["dog"], g: ["dog", "art"], dk: "2025-01-01"},
        {i: 2, t: "Paper dog collage", g: ["dog", "art"], ty: "artwork", dk: "2010-01-01"}
    ];
    assert.deepStrictEqual(items(art, "dog art"), [2, 1]);
    assert.deepStrictEqual(items(art, "dog"), [1, 2]);
    // A thing named in the title is real even when its field is empty, but
    // one only in a corner of the picture (in its food list) ranks below.
    var beers = [
        {i: 1, t: "Driver's license in a wallet", fo: ["beer"], d: "A beer in the background.",
         dk: "2025-01-01"},
        {i: 2, t: "Beer bottles on a table", d: "Three beer bottles.", dk: "2010-01-01"}
    ];
    assert.deepStrictEqual(items(beers, "beer"), [2, 1]);
    // A material only makes a depiction of the word it ends with.
    var cups = [
        {i: 1, t: "Paper coffee cup", fo: ["latte"], dk: "2015-01-01"},
        {i: 2, t: "Plastic coffee", fo: ["latte"], dk: "2014-01-01"},
        {i: 3, t: "Coffee at breakfast", fo: ["coffee"], dk: "2010-01-01"}
    ];
    assert.deepStrictEqual(items(cups, "coffee"), [3, 1, 2]);
});

test("a word is no thing because a few lists end with it", function () {
    var records = [
        {i: 1, t: "Light painting in the living room", g: ["light painting"], dk: "2015-01-01"},
        {i: 2, t: "Street at night", v: ["street light"], g: ["light"], dk: "2025-01-01"}
    ];
    for (var k = 3; k < 15; k++) {
        records.push({i: k, d: "Soft light through a window.", dk: "2010-01-01"});
    }
    // "light" ends one vehicle list, but most pictures with it are about
    // something else: no bonus, no picture of a light.
    assert.deepStrictEqual(items(records, "light").slice(0, 2), [1, 2]);
    // Words for kinds of things are things: a stuffed animal is no animal,
    // nor are animal tracks; any real animal is one.
    var animals = [
        {i: 1, t: "Stuffed animal on a bed", dk: "2025-01-01"},
        {i: 2, t: "Animal tracks in the snow", dk: "2015-01-01"},
        {i: 3, t: "Tapir in the woods", g: ["animal", "tapir"], a: ["tapir"],
         dk: "2010-01-01"}
    ];
    var found = items(animals, "animal");
    assert.strictEqual(found[0], 3);
    assert.deepStrictEqual(found.slice(1).sort(), [1, 2]);
});

test("one word only in the text of a picture ranks below pictures about it", function () {
    var records = [
        {i: 1, o: "DOG FOOD 2 FOR 1", dk: "2025-01-01"},
        {i: 2, d: "A dog asleep in the sun.", dk: "2015-01-01"}
    ];
    assert.deepStrictEqual(items(records, "dog"), [2, 1]);
    // (With more words to look for, every field counts as before.)
    assert.deepStrictEqual(items(records, "text:dog"), [1]);
    assert.deepStrictEqual(items(records, "dog food"), [1]);
});

test("videos are a kind of picture: is:video, type:video and the Type facet", function () {
    var records = [
        {i: 1, t: "Fireworks over the bay", ty: "video", mv: 1, dr: 24, vc: "hevc", ds: 1},
        {i: 2, t: "Fireworks", ty: "photo", ds: 1},
        {i: 3, n: "IMG_0003.MOV", ty: "video", mv: 1, dr: 4},
        {i: 4, t: "A chat", ty: "screenshot", ds: 1}
    ];
    assert.deepStrictEqual(items(records, "is:video").sort(), [1, 3]);
    assert.deepStrictEqual(items(records, "type:video").sort(), [1, 3]);
    assert.deepStrictEqual(items(records, "type:videos").sort(), [1, 3]);
    assert.deepStrictEqual(items(records, "is:movie").sort(), [1, 3]);
    assert.deepStrictEqual(items(records, "-is:video").sort(), [2, 4]);
    // is:photo stays the photo type: no videos, no screenshots.
    assert.deepStrictEqual(items(records, "is:photo"), [2]);
    assert.deepStrictEqual(items(records, "fireworks video"), [1]);
    var result = search(records, "");
    assert.strictEqual(counts(result, "type").video, 2);
    assert.strictEqual(SearchCore.facetLabel("type", "video"), "Videos");
    assert.deepStrictEqual(items(records, "", {filters: {type: ["video"]}}).sort(), [1, 3]);
    assert.ok(SearchCore.SYNTAX.some(function (row) {
        return row.meaning.indexOf("is:video") >= 0;
    }));
});

test("is:edited finds the pictures the owner edited", function () {
    var records = [{i: 1, t: "Rex", ed: 1}, {i: 2, t: "Beach"}];
    assert.deepStrictEqual(items(records, "is:edited"), [1]);
    assert.deepStrictEqual(items(records, "-is:edited"), [2]);
});

/* ---------- Places, dates and shuffling ----------------------------------- */

var PLACED = [
    {i: 1, t: "Beach at sunset", g: ["beach"], dk: "2019-05-14", co: "United States",
     rg: "Florida", ci: "Sarasota", cc: "US", km: 3},
    {i: 2, t: "Harbour", g: ["boat"], dk: "2019-05-20", co: "United States", rg: "Florida",
     ci: "Tampa", cc: "US", km: 1},
    {i: 3, t: "Golden Gate bridge", g: ["bridge"], dk: "2020-10-11", co: "United States",
     rg: "California", ci: "San Francisco", cc: "US", km: 2},
    {i: 4, t: "Out at sea", g: ["ocean"], dk: "2021-10-11", co: "United States",
     rg: "Florida", cc: "US"},
    {i: 5, t: "Tower", g: ["tower"], dk: "2021-10-11", co: "France", rg: "Ile-de-France",
     ci: "Paris", cc: "FR"},
    {i: 6, t: "Palace", g: ["palace"], dk: "2022-03-01", co: "Monaco", ci: "Monaco", cc: "MC"},
    {i: 7, t: "Pub", g: ["beer"], dk: "2023-10-11", co: "United Kingdom", rg: "England",
     ci: "London", cc: "GB"},
    {i: 8, t: "No place", g: ["beach"], dk: "2026-10-11"},
    {i: 9, t: "No date", g: ["beach"]}
];

function ids(result) {
    return result.items.slice().sort(function (a, b) {
        return a - b;
    });
}

test("place words find the pictures taken there", function () {
    assert.deepStrictEqual(ids(search(PLACED, "florida")), [1, 2, 4]);
    assert.deepStrictEqual(ids(search(PLACED, "sarasota")), [1]);
    assert.deepStrictEqual(ids(search(PLACED, "\"united states\"")), [1, 2, 3, 4]);
    assert.deepStrictEqual(ids(search(PLACED, "new york")), []);
    // Other names of the country.
    assert.deepStrictEqual(ids(search(PLACED, "usa")), [1, 2, 3, 4]);
    assert.deepStrictEqual(ids(search(PLACED, "america beach")), [1]);
    assert.deepStrictEqual(ids(search(PLACED, "uk")), [7]);
    assert.deepStrictEqual(ids(search(PLACED, "britain")), [7]);
    // Accents do not matter.
    assert.deepStrictEqual(ids(search(PLACED, "\u00eele-de-france")), [5]);
    // "us" alone is the country; with other words it is skipped like "me".
    assert.deepStrictEqual(ids(search(PLACED, "us")), [1, 2, 3, 4]);
    assert.deepStrictEqual(ids(search(PLACED, "pictures of us at the beach")), [1, 8, 9]);
    // place: looks at the place only.
    assert.deepStrictEqual(ids(search(PLACED, "place:beach")), []);
    assert.deepStrictEqual(ids(search(PLACED, "place:paris")), [5]);
    assert.deepStrictEqual(ids(search(PLACED, "place:\"san francisco\"")), [3]);
    assert.deepStrictEqual(ids(search(PLACED, "beach -place:florida")), [8, 9]);
    // A word in the title ranks above the same word in the place.
    var records = PLACED.concat([{i: 10, t: "Paris poster", g: ["poster"], dk: "2020-01-01"}]);
    assert.strictEqual(search(records, "paris").items[0], 10);
    // "US" in capitals is the country, also next to other words.
    assert.deepStrictEqual(ids(search(PLACED, "beach US")), [1]);
    assert.deepStrictEqual(ids(search(PLACED, "beach us")), [1, 8, 9]);
    // A lower-case "us" after other words is skipped, but the words
    // together still come first.
    var pages = [{i: 1, t: "Contact form", d: "A contact form.", o: "Contact"},
                 {i: 2, t: "Contact page", d: "A web page.", o: "Contact us"}];
    assert.deepStrictEqual(search(pages, "contact us").items, [2, 1]);
});

test("a word only in the place names ranks below the same word anywhere else", function () {
    // Town names hold everyday words: a room in Long Beach is no beach.
    var records = [
        {i: 1, t: "Living room", g: ["sofa"], d: "A sofa by the window.", dk: "2021-06-01",
         co: "United States", rg: "California", ci: "Long Beach", cc: "US", km: 1},
        {i: 2, t: "Sunset", g: ["sunset"], d: "Waves roll onto a sandy beach.", dk: "2019-06-01"},
        {i: 3, t: "Receipt", g: ["receipt"], d: "A receipt from a shop.", dk: "2022-01-01",
         co: "United States", rg: "California", ci: "Mountain View", cc: "US", km: 3},
        {i: 4, t: "Peak", g: ["snow"], d: "A snowy mountain above the clouds.",
         dk: "2018-01-01"},
        {i: 5, t: "Office", g: ["desk"], d: "A desk with a laptop.", dk: "2023-01-01",
         co: "Turkey", rg: "Istanbul", ci: "Istanbul", cc: "TR", km: 2},
        {i: 6, t: "Dinner", g: ["food"], d: "A roast turkey on the table.", dk: "2017-11-23"}
    ];
    assert.deepStrictEqual(search(records, "beach").items, [2, 1]);
    assert.deepStrictEqual(search(records, "mountain").items, [4, 3]);
    assert.deepStrictEqual(search(records, "turkey").items, [6, 5]);
    // Newest first stays newest first.
    assert.deepStrictEqual(search(records, "beach", {sort: "newest"}).items, [1, 2]);
    // place: looks at the place names only, at full weight.
    assert.deepStrictEqual(search(records, "place:beach").items, [1]);
    // A town's whole name still finds it first.
    assert.strictEqual(search(records, "long beach").items[0], 1);
});

test("the place facet is a tree that opens where a place is chosen", function () {
    var result = search(PLACED, "");
    var entries = result.facets.place;
    assert.deepStrictEqual(entries.map(function (entry) {
        return [entry.value, entry.count, entry.level];
    }), [["United States", 4, 0], ["France", 1, 0], ["Monaco", 1, 0],
         ["United Kingdom", 1, 0]]);
    result = search(PLACED, "", {filters: {place: ["United States"]}});
    assert.deepStrictEqual(ids(result), [1, 2, 3, 4]);
    assert.deepStrictEqual(result.facets.place.map(function (entry) {
        return entry.value;
    }), ["United States", "United States|Florida", "United States|California", "France",
         "Monaco", "United Kingdom"]);
    result = search(PLACED, "", {filters: {place: ["United States|Florida"]}});
    assert.deepStrictEqual(ids(result), [1, 2, 4]);
    var byValue = {};
    result.facets.place.forEach(function (entry) {
        byValue[entry.value] = entry;
    });
    assert.ok(byValue["United States"].open);
    assert.ok(!byValue["United States"].selected);
    assert.ok(byValue["United States|Florida"].selected);
    assert.strictEqual(byValue["United States|Florida|Sarasota"].level, 2);
    assert.strictEqual(byValue["United States|Florida|Sarasota"].label, "Sarasota");
    // The other countries keep their counts (the facet is disjunctive).
    assert.strictEqual(byValue.France.count, 1);
    // A town without a known region hangs under its country.
    result = search(PLACED, "", {filters: {place: ["Monaco"]}});
    assert.deepStrictEqual(result.facets.place.filter(function (entry) {
        return entry.level;
    }).map(function (entry) {
        return entry.value;
    }), ["Monaco||Monaco"]);
    assert.strictEqual(SearchCore.placeParent("Monaco||Monaco"), "Monaco");
    assert.strictEqual(SearchCore.placeParent("United States|Florida|Sarasota"),
                       "United States|Florida");
    assert.strictEqual(SearchCore.placeParent("France"), "");
    assert.strictEqual(SearchCore.placeLabel("United States|Florida|Sarasota"),
                       "Sarasota, Florida, United States");
    assert.strictEqual(SearchCore.placeLabel("Monaco||Monaco"), "Monaco, Monaco");
    assert.strictEqual(SearchCore.facetLabel("place", "United States|Florida"), "Florida");
    assert.strictEqual(SearchCore.placeOf(PLACED[0]), "United States|Florida|Sarasota");
    assert.strictEqual(SearchCore.placeOf(PLACED[3]), "United States|Florida");
    assert.strictEqual(SearchCore.placeOf(PLACED[7]), "");
    // A chosen place no picture has is still listed, under its parents.
    result = search(PLACED, "", {filters: {place: ["Japan|Tokyo"]}});
    assert.strictEqual(result.total, 0);
    assert.deepStrictEqual(result.facets.place.filter(function (entry) {
        return entry.selected || entry.open;
    }).map(function (entry) {
        return [entry.value, entry.count];
    }), [["Japan", 0], ["Japan|Tokyo", 0]]);
});

test("on: finds a day of the year", function () {
    var today = {today: "2026-10-11"};
    // This day in earlier years: not this year's.
    assert.deepStrictEqual(ids(search(PLACED, "on:today", today)), [3, 4, 5, 7]);
    assert.deepStrictEqual(search(PLACED, "on:today", today).items, [7, 5, 4, 3]);
    assert.deepStrictEqual(ids(search(PLACED, "on:10-11")), [3, 4, 5, 7, 8]);
    assert.deepStrictEqual(ids(search(PLACED, "on:5-14")), [1]);
    assert.deepStrictEqual(ids(search(PLACED, "on:2021-10-11")), [4, 5]);
    assert.deepStrictEqual(ids(search(PLACED, "on:today place:usa", today)), [3, 4]);
    assert.deepStrictEqual(ids(search(PLACED, "beach -on:today", today)), [1, 8, 9]);
    ["on:13-01", "on:02-32", "on:tomorrow", "on:2019"].forEach(function (query) {
        var result = search(PLACED, query, today);
        assert.strictEqual(result.warnings.length, 1, query);
        assert.ok(/on:today/.test(result.warnings[0]), query);
    });
    // Without today, this computer's date.
    assert.strictEqual(search(PLACED, "on:today").warnings.length, 0);
});

test("the range option keeps the pictures taken in it", function () {
    function within(from, to) {
        return ids(search(PLACED, "", {range: {from: from, to: to}}));
    }
    assert.deepStrictEqual(within("2019", "2019"), [1, 2]);
    assert.deepStrictEqual(within("2019-05-15", "2019-05"), [2]);
    assert.deepStrictEqual(within("2020", "2021-10-11"), [3, 4, 5]);
    assert.deepStrictEqual(within("2022", ""), [6, 7, 8]);
    assert.deepStrictEqual(within("", "2019-05-14"), [1]);
    // Backwards, it is turned round; nonsense is no range.
    assert.deepStrictEqual(within("2021", "2020"), [3, 4, 5]);
    assert.strictEqual(search(PLACED, "", {range: {from: "2019-13", to: "x"}}).total, 9);
    assert.strictEqual(search(PLACED, "", {range: "2019"}).total, 9);
    // The facets count within it.
    var result = search(PLACED, "", {range: {from: "2019", to: "2019"}});
    assert.deepStrictEqual(counts(result, "year"), {"2019": 2});
});

test("the timeline counts per year, per month of one year, per day of one month", function () {
    // Only when asked for (the search page; not the map or the empty-results notes).
    assert.strictEqual(SearchCore.search(PLACED, "", {}).timeline, null);
    function timed(records, query, options) {
        var asked = {timeline: true};
        Object.keys(options || {}).forEach(function (key) {
            asked[key] = options[key];
        });
        return SearchCore.search(records, query, asked);
    }
    var line = timed(PLACED, "").timeline;
    assert.strictEqual(line.level, "year");
    assert.strictEqual(line.dated, 8);
    assert.strictEqual(line.undated, 1);
    assert.deepStrictEqual(line.bars.map(function (bar) {
        return bar.key + ":" + bar.count;
    }), ["2019:2", "2020:1", "2021:2", "2022:1", "2023:1", "2024:0", "2025:0", "2026:1"]);
    line = timed(PLACED, "", {range: {from: "2019", to: "2019-12"}}).timeline;
    assert.strictEqual(line.level, "day");
    assert.strictEqual(line.bars.length, 31);
    assert.strictEqual(line.bars[13].key, "2019-05-14");
    assert.strictEqual(line.bars[13].count, 1);
    assert.strictEqual(line.undated, 0);
    var records = PLACED.concat([{i: 20, dk: "2019-02-03"}]);
    line = timed(records, "", {range: {from: "2019", to: "2019"}}).timeline;
    assert.strictEqual(line.level, "month");
    assert.deepStrictEqual(line.bars.map(function (bar) {
        return bar.count;
    }), [0, 1, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0]);
    // February has 28 or 29 days.
    line = timed([{i: 1, dk: "2024-02-01"}, {i: 2, dk: "2024-02-29"}], "").timeline;
    assert.strictEqual(line.bars.length, 29);
    line = timed([{i: 1, dk: "2023-02-01"}], "").timeline;
    assert.strictEqual(line.bars.length, 28);
    // Nothing dated: no timeline.
    line = timed([{i: 1}, {i: 2, dk: "x"}], "").timeline;
    assert.deepStrictEqual(line, {level: null, bars: [], dated: 0, undated: 2});
    // The matches of the query, before duplicates are stacked.
    line = timed(PLACED, "florida").timeline;
    assert.deepStrictEqual(line.bars.map(function (bar) {
        return bar.key;
    }), ["2019", "2020", "2021"]);
});

test("a date that is only the file's time is no date taken", function () {
    // (du: the server's records of pictures dated by their file's time.)
    var records = PLACED.concat([{i: 30, t: "Copied", g: ["beach"], dk: "2021-10-11", du: 1},
                                 {i: 31, t: "Copied", g: ["beach"], dk: "2016-03-02", du: 1}]);
    var today = {today: "2026-10-11"};
    assert.deepStrictEqual(ids(search(records, "on:today", today)), [3, 4, 5, 7]);
    assert.deepStrictEqual(ids(search(records, "on:2021-10-11")), [4, 5]);
    assert.deepStrictEqual(ids(search(records, "", {range: {from: "2021", to: "2021"}})),
                           [4, 5]);
    var line = search(records, "", {timeline: true}).timeline;
    assert.strictEqual(line.dated, 8);
    assert.strictEqual(line.undated, 3);
    assert.strictEqual(line.bars[0].key, "2019");
    // The other date filters keep them (as before).
    assert.deepStrictEqual(ids(search(records, "year:2016")), [31]);
});

test("choosing bars of the timeline, with Shift for a range", function () {
    var bars = [{key: "2015", count: 3}, {key: "2016", count: 0}, {key: "2017", count: 2},
                {key: "2018", count: 1}];
    var none = {anchor: "", pending: false};
    // A bar alone; it becomes the anchor.
    assert.deepStrictEqual(SearchCore.timelineChoice(bars, 2, false, none),
                           {anchor: "2017", pending: false, from: "2017", to: "2017"});
    // Shift with no anchor among the bars: the first end, nothing chosen yet ...
    var first = SearchCore.timelineChoice(bars, 0, true, {anchor: "2019-05", pending: false});
    assert.deepStrictEqual(first, {anchor: "2015", pending: true});
    // ... and Shift on another bar chooses the bars from one to the other.
    assert.deepStrictEqual(SearchCore.timelineChoice(bars, 3, true, first),
                           {anchor: "2015", pending: false, from: "2015", to: "2018"});
    assert.deepStrictEqual(SearchCore.timelineChoice(bars, 0, true,
                                                     {anchor: "2018", pending: true}),
                           {anchor: "2018", pending: false, from: "2015", to: "2018"});
    // Shift on the first end again: that bar alone.
    assert.deepStrictEqual(SearchCore.timelineChoice(bars, 0, true, first),
                           {anchor: "2015", pending: false, from: "2015", to: "2015"});
    // A range may end at a bar without pictures; that bar alone is nothing.
    assert.deepStrictEqual(SearchCore.timelineChoice(bars, 1, true, first),
                           {anchor: "2015", pending: false, from: "2015", to: "2016"});
    assert.strictEqual(SearchCore.timelineChoice(bars, 1, false, first), null);
    assert.strictEqual(SearchCore.timelineChoice(bars, 1, true, none), null);
    assert.strictEqual(SearchCore.timelineChoice(bars, 9, false, none), null);
});

test("dates in words", function () {
    assert.strictEqual(SearchCore.dateLabel("2019"), "2019");
    assert.strictEqual(SearchCore.dateLabel("2019-05"), "May 2019");
    assert.strictEqual(SearchCore.dateLabel("2019-05-04"), "May 4, 2019");
    assert.strictEqual(SearchCore.dateLabel("2019-13"), "");
    var dash = " \u2013 ";
    assert.strictEqual(SearchCore.rangeLabel("2015", "2017"), "2015" + dash + "2017");
    assert.strictEqual(SearchCore.rangeLabel("2019-03", "2019-05"), "March" + dash + "May 2019");
    assert.strictEqual(SearchCore.rangeLabel("2019-05-03", "2019-05-09"),
                       "May 3" + dash + "9, 2019");
    assert.strictEqual(SearchCore.rangeLabel("2019-05-03", "2019-06-09"),
                       "May 3" + dash + "June 9, 2019");
    assert.strictEqual(SearchCore.rangeLabel("2019-11", "2020-02"),
                       "November 2019" + dash + "February 2020");
    assert.strictEqual(SearchCore.rangeLabel("2019-05", "2019-05"), "May 2019");
    assert.strictEqual(SearchCore.rangeLabel("2019", ""), "since 2019");
    assert.strictEqual(SearchCore.rangeLabel("", "2019-05"), "until May 2019");
    assert.strictEqual(SearchCore.rangeLabel("", ""), "");
});

test("shuffle: a random order that stays the same for a seed", function () {
    var records = makeRecords(500, 5);
    var index = SearchCore.prepare(records);
    var first = index.search("", {sort: "shuffle", seed: 42}).items;
    var again = index.search("", {sort: "shuffle", seed: 42}).items;
    var other = index.search("", {sort: "shuffle", seed: 43}).items;
    var newest = index.search("", {sort: "newest"}).items;
    assert.strictEqual(first.length, newest.length);
    assert.deepStrictEqual(first, again);
    assert.notDeepStrictEqual(first, other);
    assert.notDeepStrictEqual(first, newest);
    assert.deepStrictEqual(first.slice().sort(), newest.slice().sort());
    // A search within it keeps the shuffled order of what it finds.
    var beach = index.search("beach", {sort: "shuffle", seed: 42});
    var place = {};
    first.forEach(function (id, at) {
        place[id] = at;
    });
    for (var k = 1; k < beach.items.length; k++) {
        assert.ok(place[beach.items[k - 1]] < place[beach.items[k]]);
    }
    assert.strictEqual(beach.sort, "shuffle");
    // Spread evenly: the first half holds about half of the older pictures.
    var older = newest.slice(newest.length / 2);
    var early = first.slice(0, first.length / 2).filter(function (id) {
        return older.indexOf(id) >= 0;
    }).length;
    assert.ok(early > 90 && early < 160, String(early));
    assert.ok(SearchCore.SORTS.indexOf("shuffle") >= 0);
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
var PLACES = [["United States", "US", "California", "Oakland"],
              ["Portugal", "PT", "Algarve", "Faro"], ["France", "FR", "Bretagne", "Brest"],
              ["Japan", "JP", "Tokyo", "Shinjuku"], ["Singapore", "SG", "", "Singapore"],
              ["Iceland", "IS", "Capital Region", ""]];

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
            var place = pick(PLACES);
            rec.co = place[0];
            rec.cc = place[1];
            if (place[2]) {
                rec.rg = place[2];
            }
            if (place[3] && random() < 0.8) {
                rec.ci = place[3];
                rec.km = Math.floor(random() * 40);
            }
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
                   "camera:iphone after:2019", "is:duplicate", "red blue green yellow",
                   "place:tokyo", "usa beach", "on:05-14", "on:today"];
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

/* ---------- Search by meaning ---------------------------------------------- */

var MEANING_DIMS = 8;

// A signed-byte vector (unit length times 127) from numbers.
function byteVector(values) {
    var norm = Math.sqrt(values.reduce(function (sum, value) {
        return sum + value * value;
    }, 0));
    var out = new Int8Array(MEANING_DIMS);
    values.forEach(function (value, k) {
        out[k] = Math.round(value / norm * 127);
    });
    return out;
}

// A vector whose cosine with the query (axis 0) is cos, leaning towards axis.
function closeTo(cos, axis) {
    var values = [];
    for (var k = 0; k < MEANING_DIMS; k++) {
        values.push(0);
    }
    values[0] = cos;
    values[axis || 1] = Math.sqrt(Math.max(0, 1 - cos * cos));
    return byteVector(values);
}

var QUERY = closeTo(1);

// An index of records with vectors ({id: Int8Array}), among 200 (or
// fillers) pictures that are not what is looked for: their cosines with
// the query (axis 0) spread from 0.05 to 0.45, as most of a real index's
// are. (Cosines are judged against those of the whole index.)
function meaningIndex(records, vectors, fillers) {
    fillers = fillers || 200;
    for (var k = 0; k < fillers; k++) {
        var id = 10000 + k;
        records.push({i: id, t: "Filler picture " + k, dk: "2010-01-01", ds: 1});
        vectors[id] = closeTo(0.05 + 0.4 * (k / (fillers - 1)), 6 + (k % 2));
    }
    var ids = Object.keys(vectors).map(Number);
    var data = new Int8Array(ids.length * MEANING_DIMS);
    ids.forEach(function (id, k) {
        data.set(vectors[id], k * MEANING_DIMS);
    });
    var index = SearchCore.prepare(records);
    var known = {};
    records.forEach(function (rec) {
        known[rec.i] = true;
    });
    assert.strictEqual(index.setVectors({dims: MEANING_DIMS, ids: ids, data: data}),
                       ids.filter(function (id) {
                           return known[id];
                       }).length);
    return index;
}

test("meaning vectors are decoded, joined and checked", function () {
    var bytes = new Int8Array([1, -2, 3, -4, 5, -6]);
    var text = Buffer.from(bytes.buffer).toString("base64");
    var decoded = SearchCore.decodeVectors({model: "m", dims: 3, query: true, ids: [7, 8],
                                            data: text});
    assert.deepStrictEqual(Array.from(decoded.data), [1, -2, 3, -4, 5, -6]);
    assert.deepStrictEqual([decoded.model, decoded.dims, decoded.query, decoded.ids],
                           ["m", 3, true, [7, 8]]);
    // The wrong length, no ids, a bad size: nothing.
    assert.strictEqual(SearchCore.decodeVectors({dims: 4, ids: [7, 8], data: text}), null);
    assert.strictEqual(SearchCore.decodeVectors({dims: 3, ids: [], data: ""}), null);
    assert.strictEqual(SearchCore.decodeVectors({dims: 0, ids: [7], data: text}), null);
    assert.strictEqual(SearchCore.decodeVectors(null), null);
    var other = SearchCore.decodeVectors({dims: 3, ids: [9], data: new Int8Array([9, 9, 9])});
    var joined = SearchCore.joinVectors([decoded, null, other,
                                         SearchCore.decodeVectors({dims: 2, ids: [1],
                                                                   data: new Int8Array(2)})]);
    assert.deepStrictEqual(joined.ids, [7, 8, 9]);
    assert.deepStrictEqual(Array.from(joined.data), [1, -2, 3, -4, 5, -6, 9, 9, 9]);
    assert.strictEqual(SearchCore.joinVectors([]), null);
    assert.deepStrictEqual(Array.from(SearchCore.queryVector(
        Buffer.from(new Int8Array([5, -5]).buffer).toString("base64"), 2)), [5, -5]);
    assert.strictEqual(SearchCore.queryVector("AAAA", 2), null);
});

test("the text whose meaning a query looks for", function () {
    assert.strictEqual(SearchCore.meaningText("kids playing in the snow"),
                       "kids playing in the snow");
    assert.strictEqual(SearchCore.meaningText("year:2019 -cat tag:dog \"red car\" beach"),
                       "red car beach");
    assert.strictEqual(SearchCore.meaningText("beach no people"), "beach");
    assert.strictEqual(SearchCore.meaningText("year:2019 type:photo"), "");
    assert.strictEqual(SearchCore.meaningText("\"red car\""), "");
    assert.strictEqual(SearchCore.meaningText("similar:5 beach"), "");
    assert.strictEqual(SearchCore.meaningText(""), "");
});

test("words and meaning: word matches first, then pictures that mean the same", function () {
    // (Words with no synonyms, so only meaning links them.)
    var records = [
        {i: 1, t: "Zebra on a quilt", g: ["zebra", "quilt"], dk: "2020-01-01", ds: 1},
        {i: 2, t: "Striped animal resting on a bedspread", g: ["bedspread"], dk: "2021-01-01",
         ds: 1},
        {i: 3, t: "Zebra in the savanna", g: ["zebra", "savanna"], dk: "2020-01-03", ds: 1},
        {i: 4, t: "Birthday cake", g: ["cake"], dk: "2020-01-04", ds: 1},
        {i: 5, t: "A quilt and a zebra mug", g: ["quilt", "mug"], dk: "2020-01-05", ds: 1},
        {i: 6, t: "Stripes on a bedspread", g: ["bedspread"], dk: "2020-01-06", ds: 1},
        {i: 7, t: "Old bedspread", dk: "2020-01-07", ds: 1}
    ];
    var index = meaningIndex(records, {1: closeTo(0.9), 2: closeTo(0.85, 2), 3: closeTo(0.55),
                                       4: closeTo(0.2, 3), 5: closeTo(0.4, 4),
                                       6: closeTo(0.65, 5)});
    var result = index.search("zebra quilt", {vector: QUERY});
    // Both word matches (the closer one first), then the striped animal by
    // meaning; the zebra in the savanna lacks a word and is too far below
    // the best cosine, and so is the other bedspread.
    assert.deepStrictEqual(result.items, [1, 5, 2]);
    assert.deepStrictEqual(result.byMeaning, [2]);
    assert.strictEqual(result.meaning.mode, "hybrid");
    assert.ok(Math.abs(result.meaning.best - 0.9) < 0.02, result.meaning.best);
    assert.strictEqual(result.sort, "relevance");
    assert.strictEqual(result.total, 3);
    // Words only: as without a vector.
    assert.deepStrictEqual(index.search("zebra quilt", {vector: QUERY, meaning: "words"}).items,
                           [1, 5]);
    var plain = index.search("zebra quilt");
    assert.deepStrictEqual(plain.items, [1, 5]);
    assert.deepStrictEqual(plain.byMeaning, []);
    assert.strictEqual(plain.meaning, null);
    // A vector of another size is not used.
    assert.deepStrictEqual(index.search("zebra quilt", {vector: new Int8Array(3)}).items, [1, 5]);
    // Meaning only: the closest, words or not (a wider net: three standard
    // deviations of the index's cosines below the best).
    assert.deepStrictEqual(index.search("zebra quilt", {vector: QUERY, meaning: "meaning"})
                           .items, [1, 2, 6]);
    // Sorted by date, the same pictures.
    assert.deepStrictEqual(index.search("zebra quilt", {vector: QUERY, sort: "newest"}).items,
                           [2, 5, 1]);
    // Filters only: nothing for meaning to rank.
    assert.strictEqual(index.search("year:2020", {vector: QUERY}).meaning, null);
});

test("did you mean: a correction must find pictures by its words, not by meaning", function () {
    // (The query's meaning vector is of the words as typed: whatever the
    // corrected words are, meaning would find the same pictures.)
    var records = [
        {i: 1, t: "Striped animal resting on a bedspread", g: ["bedspread"], dk: "2020-01-01",
         ds: 1},
        {i: 2, t: "A line of trees", g: ["line", "trees"], dk: "2020-01-02", ds: 1}
    ];
    var index = meaningIndex(records, {1: closeTo(0.9), 2: closeTo(0.2, 2)});
    assert.deepStrictEqual(index.search("feline lounging", {vector: QUERY}).items, [1]);
    // "line lounging" has no word match either: nothing is offered.
    assert.strictEqual(index.suggest("feline lounging", {vector: QUERY}), null);
    assert.strictEqual(index.suggest("feline lounging", {vector: QUERY, meaning: "meaning"}),
                       null);
    // A real typo is still corrected with meaning on.
    assert.strictEqual(index.suggest("bedsprea", {vector: QUERY}).text, "bedspread");
});

test("a word match about something else ranks below the word matches that mean it", function () {
    var records = [
        {i: 1, t: "Zebra on a quilt", g: ["zebra", "quilt"], dk: "2020-01-01", ds: 1},
        {i: 2, t: "Striped animal resting on a bedspread", g: ["bedspread"], dk: "2020-01-02",
         ds: 1},
        {i: 3, t: "Zebra quilt sale flyer", g: ["zebra", "quilt"], dk: "2020-01-03", ds: 1},
        {i: 4, t: "Zebra quilt", dk: "2020-01-04"},
        {i: 5, t: "Shop window", d: "A sign in the window mentions a zebra quilt.",
         dk: "2020-01-05", ds: 1},
        {i: 6, t: "Zebra pattern on a quilt", g: ["zebra", "quilt"], dk: "2020-01-06", ds: 1}
    ];
    // (Picture 4 has no vector yet: its words alone count.)
    var index = meaningIndex(records, {1: closeTo(0.9), 2: closeTo(0.88, 2),
                                       3: closeTo(-0.3, 3), 5: closeTo(-0.3, 4),
                                       6: closeTo(0.8, 5)});
    var result = index.search("zebra quilt", {vector: QUERY, sort: "relevance"});
    var at = function (id) {
        return result.items.indexOf(id);
    };
    assert.deepStrictEqual(result.items.slice(0, 3), [1, 6, 4]);
    // Strong words stay above what only means the same; weak words about
    // something else drop below it.
    assert.ok(at(3) < at(2) && at(2) < at(5), result.items.join(" "));
    assert.strictEqual(result.total, 6);
    assert.deepStrictEqual(result.byMeaning, [2]);
    // (Sorting and stacks take the scores as they are: none below zero.)
    assert.deepStrictEqual(index.search("zebra quilt", {vector: QUERY, group: true}).items,
                           result.items);
});

test("pictures found by meaning alone come after the word matches that mean it", function () {
    var records = [
        {i: 1, t: "Snowman in the yard", g: ["snowman"], dk: "2020-01-01", ds: 1},
        {i: 2, d: "A snowman, just the words in a corner.", dk: "2020-01-02", ds: 1},
        {i: 3, t: "Children building a snow figure", g: ["snow"], dk: "2020-01-03", ds: 1}
    ];
    // The weak word match (2) is still about the query (z about 3); the
    // picture without the word is the closest of all.
    var index = meaningIndex(records, {1: closeTo(0.85), 2: closeTo(0.6, 2),
                                       3: closeTo(0.95, 3)});
    var result = index.search("snowman", {vector: QUERY});
    assert.deepStrictEqual(result.items, [1, 2, 3]);
    assert.deepStrictEqual(result.byMeaning, [3]);
});

test("names: when the words found pictures unlike the query, meaning adds nothing", function () {
    // A place name: the word is in the pictures' text, but the model makes
    // something else of it (their cosines are those of any picture).
    var records = [
        {i: 1, t: "Mortgage statement", x: "Maplebrook branch", dk: "2020-01-01", ds: 1},
        {i: 2, t: "Fence along a pond", pl: "Maplebrook", dk: "2020-01-02", ds: 1},
        {i: 3, t: "Maplebrook Maplebrook Maplebrook", dk: "2020-01-03", ds: 1},
        {i: 4, t: "Maple leaf brooch carving", g: ["carving"], dk: "2020-01-04", ds: 1}
    ];
    var index = meaningIndex(records, {1: closeTo(0.2, 2), 2: closeTo(0.28, 3),
                                       3: closeTo(0.24, 4), 4: closeTo(0.9, 5)});
    var words = index.search("Maplebrook");
    var result = index.search("Maplebrook", {vector: QUERY});
    assert.deepStrictEqual(result.items, words.items);
    assert.deepStrictEqual(result.byMeaning, []);
    assert.strictEqual(result.meaning, null);
    assert.ok(result.items.indexOf(4) < 0);
    // By meaning only, the model's view decides (the switch asked for it).
    assert.deepStrictEqual(index.search("Maplebrook", {vector: QUERY, meaning: "meaning"}).items,
                           [4]);
});

test("at most 40 pictures are added by meaning alone: the closest", function () {
    var records = [];
    var vectors = {};
    for (var k = 0; k < 60; k++) {
        records.push({i: k + 1, t: "Picture " + k, dk: "2020-01-01", ds: 1});
        vectors[k + 1] = closeTo(0.9 - 0.11 * (k / 59), 1 + (k % 5));
    }
    var index = meaningIndex(records, vectors, 2000);
    var result = index.search("snowmen", {vector: QUERY});
    assert.strictEqual(result.total, 40);
    // (The closest by their stored, rounded, vectors.)
    var cos = index.cosineStats(QUERY).values;
    var closest = records.slice(0, 60).map(function (rec, row) {
        return {id: rec.i, cos: cos[row], row: row};
    }).sort(function (a, b) {
        return b.cos - a.cos || a.row - b.row;
    }).slice(0, 40).map(function (entry) {
        return entry.id;
    });
    assert.deepStrictEqual(result.byMeaning, closest);
    assert.deepStrictEqual(result.items.slice().sort(function (a, b) {
        return a - b;
    }), closest.slice().sort(function (a, b) {
        return a - b;
    }));
    assert.ok(closest.indexOf(60) < 0 && closest.indexOf(1) >= 0);
});

test("filters, phrases, field words and -words stay strict with meaning", function () {
    var records = [
        {i: 1, t: "Zebra asleep on a quilt", g: ["zebra", "quilt"], dk: "2020-01-01", ds: 1,
         ty: "photo"},
        {i: 2, t: "Striped animal resting on a bedspread", g: ["bedspread"], dk: "2021-01-01",
         ds: 1, ty: "photo"},
        {i: 3, t: "Drawing of a striped animal on a bedspread", g: ["drawing"],
         dk: "2021-01-02", ds: 1, ty: "artwork"}
    ];
    var index = meaningIndex(records, {1: closeTo(0.9), 2: closeTo(0.85, 2), 3: closeTo(0.84, 3)});
    assert.deepStrictEqual(index.search("zebra quilt", {vector: QUERY}).items, [1, 2, 3]);
    assert.deepStrictEqual(index.search("zebra quilt year:2021", {vector: QUERY}).items, [2, 3]);
    assert.deepStrictEqual(index.search("zebra quilt type:photo", {vector: QUERY}).items, [1, 2]);
    assert.deepStrictEqual(index.search("zebra quilt -bedspread", {vector: QUERY}).items, [1]);
    assert.deepStrictEqual(index.search("tag:zebra quilt", {vector: QUERY}).items, [1]);
    assert.deepStrictEqual(index.search("\"asleep on\"", {vector: QUERY}).items, [1]);
    assert.deepStrictEqual(index.search("zebra quilt", {vector: QUERY,
                                                        filters: {type: ["artwork"]}}).items,
                           [3]);
    // The facets count the pictures found by meaning too.
    var result = index.search("zebra quilt", {vector: QUERY});
    assert.deepStrictEqual(counts(result, "type"), {photo: 2, artwork: 1});
    // Hidden pictures stay hidden.
    records[1].h = 1;
    assert.deepStrictEqual(index.search("zebra quilt", {vector: QUERY}).items, [1, 3]);
});

test("similar: pictures closest in meaning to one picture, that one first", function () {
    var records = [
        {i: 1, t: "Sunset over the sea", g: ["sunset", "sea"], dk: "2020-01-01", ds: 1},
        {i: 2, t: "Evening at the beach", g: ["beach"], dk: "2020-01-02", ds: 1},
        {i: 3, t: "Waves at dusk", g: ["waves"], dk: "2019-01-03", ds: 1},
        {i: 4, t: "Birthday cake", g: ["cake"], dk: "2020-01-04", ds: 1},
        {i: 5, t: "Undescribed", dk: "2020-01-05"},
        {i: 6, t: "Sea at sunset, hidden", g: ["sea", "sunset"], dk: "2020-01-06", ds: 1, h: 1}
    ];
    var index = meaningIndex(records, {1: closeTo(1), 2: closeTo(0.8, 2), 3: closeTo(0.7, 3),
                                       4: closeTo(0.2, 4), 6: closeTo(0.95, 5)});
    var result = index.search("similar:1");
    assert.deepStrictEqual(result.items, [1, 2, 3]);
    assert.strictEqual(result.sort, "relevance");
    assert.strictEqual(result.meaning.mode, "similar");
    assert.deepStrictEqual(result.byMeaning, []);
    assert.deepStrictEqual(index.search("similar:1", {hidden: true}).items, [1, 6, 2, 3]);
    assert.deepStrictEqual(index.search("similar:1 year:2020").items, [1, 2]);
    assert.deepStrictEqual(index.search("similar:1 waves").items, [3]);
    assert.deepStrictEqual(index.search("similar:1", {sort: "oldest"}).items, [3, 1, 2]);
    assert.deepStrictEqual(index.search("like:2").items.slice(0, 2), [2, 1]);
    var unknown = index.search("similar:999");
    assert.deepStrictEqual(unknown.items, []);
    assert.ok(/no picture 999/.test(unknown.warnings.join(" ")), unknown.warnings);
    assert.ok(/not understood/.test(index.search("similar:a b").warnings.join(" ")) ||
              index.search("similar:a b").items.length === 0);
    // The strip in the viewer: the closest by meaning, without the picture itself.
    assert.deepStrictEqual(index.similar(1, 8), [2, 3]);
    assert.deepStrictEqual(index.similar(1, 1), [2]);
});

test("similar: in a small set, the closest pictures (z 3 is out of reach)", function () {
    // Nine pictures, as in a small export: with eight others no picture can
    // be three deviations above them, however close it is. So few pictures
    // cannot tell the typical cosine of two pictures either: it is taken to
    // be 0.4, give or take 0.1, so close means at least 0.6, within 0.1 of
    // the closest.
    function smallIndex(cosines) {
        var records = [];
        var data = new Int8Array(cosines.length * MEANING_DIMS);
        var ids = [];
        cosines.forEach(function (cos, k) {
            records.push({i: k + 1, t: "Picture " + (k + 1), dk: "2020-01-0" + (k + 1), ds: 1});
            ids.push(k + 1);
            data.set(closeTo(cos, 1 + k), k * MEANING_DIMS);
        });
        var index = SearchCore.prepare(records);
        assert.strictEqual(index.setVectors({dims: MEANING_DIMS, ids: ids, data: data}),
                           cosines.length);
        return index;
    }
    // A set of one theme, all of it close: the closest four.
    var index = smallIndex([1, 0.9, 0.88, 0.86, 0.84, 0.82, 0.65, 0.3, 0.2]);
    assert.deepStrictEqual(index.search("similar:1").items, [1, 2, 3, 4, 5]);
    assert.deepStrictEqual(index.similar(1, 8), [2, 3, 4, 5]);
    assert.deepStrictEqual(index.similar(1, 2), [2, 3]);
    // (Of the pictures the rest of the search allows.)
    assert.deepStrictEqual(index.search("similar:1 year:2020").items, [1, 2, 3, 4, 5]);
    // Only those within SIMILAR_MARGIN of the closest...
    index = smallIndex([1, 0.93, 0.88, 0.7, 0.5, 0.45, 0.4, 0.35, 0.3]);
    assert.deepStrictEqual(index.search("similar:1").items, [1, 2, 3]);
    assert.deepStrictEqual(index.similar(1, 8), [2, 3]);
    // ...and at least SIMILAR_NEAR close.
    index = smallIndex([1, 0.66, 0.57, 0.5, 0.45, 0.4, 0.35, 0.3, 0.25]);
    assert.deepStrictEqual(index.similar(1, 8), [2]);
    // Two pictures: the other one, when it is close.
    assert.deepStrictEqual(smallIndex([1, 0.7]).similar(1, 8), [2]);
    assert.deepStrictEqual(smallIndex([1, 0.4]).similar(1, 8), []);
    assert.deepStrictEqual(smallIndex([1]).search("similar:1").items, [1]);
});

test("similar: a set of one theme still has pictures that stand out", function () {
    // Sixty pictures that are all rather alike (two taken at random: a
    // cosine of 0.75), as a library of one theme or only documents is; three
    // of them are closer still to the first. Judged by the set's own typical
    // cosine, none would be close; it counts as at most 0.4 (give or take
    // 0.1), as in a varied set.
    var dims = 64;
    function vector(values) {
        var norm = Math.sqrt(values.reduce(function (sum, value) {
            return sum + value * value;
        }, 0));
        var out = new Int8Array(dims);
        values.forEach(function (value, k) {
            out[k] = Math.round(value / norm * 127);
        });
        return out;
    }
    var records = [];
    var ids = [];
    var data = new Int8Array(60 * dims);
    var near = [0.8, 0.79, 0.78];
    for (var k = 0; k < 60; k++) {
        var values = [];
        for (var d = 0; d < dims; d++) {
            values.push(0);
        }
        if (k === 0) {
            values[0] = 1;
        } else {
            var cos = k <= near.length ? near[k - 1] : 0.55 + 0.15 * (k % 7) / 6;
            values[0] = cos;
            values[1] = 0.6;
            values[2 + k] = Math.sqrt(Math.max(0, 1 - cos * cos - 0.36));
        }
        records.push({i: k + 1, t: "Picture " + (k + 1), dk: "2020-01-01", ds: 1});
        ids.push(k + 1);
        data.set(vector(values), k * dims);
    }
    var index = SearchCore.prepare(records);
    index.setVectors({dims: dims, ids: ids, data: data});
    assert.strictEqual(index.pairSpread().mean, 0.4);
    assert.ok(index.pairSpread().std <= 0.1);
    assert.deepStrictEqual(index.similar(1, 8), [2, 3, 4]);
    assert.deepStrictEqual(index.search("similar:1").items, [1, 2, 3, 4]);
});

test("a typed search in a small set: the closest few when none stands out", function () {
    // Eighteen pictures, as an export of a small library has: with seventeen
    // others no picture is far above them, and a query that means two of
    // them puts both a little above the rest (z 2.3 and 2.2): they are found,
    // by words and meaning and by meaning only. An unrelated query (z 2.0 at
    // best) finds nothing, and neither does the same in a set of 62 pictures.
    function set(cosines) {
        var records = [];
        var ids = [];
        var data = new Int8Array(cosines.length * MEANING_DIMS);
        cosines.forEach(function (cos, k) {
            records.push({i: k + 1, t: "Picture " + (k + 1), dk: "2020-01-01", ds: 1});
            ids.push(k + 1);
            data.set(closeTo(cos, 1 + (k % 5)), k * MEANING_DIMS);
        });
        var index = SearchCore.prepare(records);
        index.setVectors({dims: MEANING_DIMS, ids: ids, data: data});
        return index;
    }
    function found(cosines) {
        var index = set(cosines);
        var hybrid = index.search("zebra", {vector: QUERY});
        return [hybrid.items, hybrid.byMeaning,
                index.search("zebra", {vector: QUERY, meaning: "meaning"}).items];
    }
    var rest = [];
    var wide = [];
    for (var k = 0; k < 60; k++) {
        if (k < 16) {
            rest.push(0.1 + 0.2 * k / 15);
        }
        wide.push(0.05 + 0.3 * (k % 30) / 29);
    }
    assert.deepStrictEqual(found([0.45, 0.43].concat(rest)), [[1, 2], [1, 2], [1, 2]]);
    assert.deepStrictEqual(found([0.36, 0.33].concat(rest)), [[], [], []]);
    assert.deepStrictEqual(found([0.45, 0.43].concat(wide)), [[], [], []]);
    // Only those near the closest one (z 2.7 and 1.5 here).
    assert.deepStrictEqual(found([0.47, 0.36].concat(rest)), [[1], [1], [1]]);
    // Five as close as each other do not stand out (z 1.6).
    assert.deepStrictEqual(found([0.45, 0.45, 0.45, 0.45, 0.45].concat(rest))[0], []);
});

test("the typical cosine of two pictures: worked out, or a default", function () {
    var records = [];
    var vectors = {};
    for (var k = 0; k < 60; k++) {
        records.push({i: k + 1, t: "Picture " + (k + 1), dk: "2020-01-01", ds: 1});
        vectors[k + 1] = closeTo(0.2 + 0.6 * (k % 7) / 6, 1 + (k % 5));
    }
    var ids = Object.keys(vectors).map(Number);
    var data = new Int8Array(ids.length * MEANING_DIMS);
    ids.forEach(function (id, k) {
        data.set(vectors[id], k * MEANING_DIMS);
    });
    var index = SearchCore.prepare(records);
    index.setVectors(SearchCore.decodeVectors({dims: MEANING_DIMS, ids: ids, data: data}));
    var worked = index.pairSpread();
    assert.ok(worked.mean > 0.05 && worked.mean < 0.6, JSON.stringify(worked));
    assert.ok(worked.std > 0.02, JSON.stringify(worked));
    // The same pairs every time; never more than a varied set's (mean 0.4,
    // deviation 0.1).
    index.setVectors(SearchCore.decodeVectors({dims: MEANING_DIMS, ids: ids, data: data}));
    assert.deepStrictEqual(index.pairSpread(), worked);
    assert.ok(worked.mean <= 0.4 && worked.std <= 0.1, JSON.stringify(worked));
    // Too few pictures to say: the default.
    var small = SearchCore.prepare(records.slice(0, 3));
    small.setVectors(SearchCore.decodeVectors({dims: MEANING_DIMS, ids: ids.slice(0, 3),
                                               data: data.subarray(0, 3 * MEANING_DIMS)}));
    assert.deepStrictEqual(small.pairSpread(), {mean: 0.4, std: 0.1});
});

test("similar: without a vector, the pictures sharing tags", function () {
    var records = [
        {i: 1, g: ["beach", "sea"], sc: "beach", dk: "2020-01-01", ds: 1},
        {i: 2, g: ["beach", "sea", "sand"], sc: "beach", dk: "2021-05-05", ds: 1},
        {i: 3, g: ["cake"], dk: "2015-03-03", ds: 1}
    ];
    // Vectors for other pictures only (picture 1 has none).
    var index = meaningIndex(records, {2: closeTo(0.9), 3: closeTo(0.1, 2)});
    assert.deepStrictEqual(index.search("similar:1").items, [1, 2]);
    // An index without vectors at all (an export without them).
    assert.deepStrictEqual(SearchCore.search(records.slice(), "similar:2").items, [2, 1]);
});

test("10k pictures by meaning: 256-number vectors compared under 15 ms", function () {
    var dims = 256;
    var count = 10000;
    var random = randomSource(7);
    var records = [];
    var data = new Int8Array(count * dims);
    var ids = [];
    for (var r = 0; r < count; r++) {
        records.push({i: r + 1, t: "picture " + r, ds: 1, dk: "2020-01-01"});
        ids.push(r + 1);
        for (var d = 0; d < dims; d++) {
            data[r * dims + d] = Math.round((random() - 0.5) * 30);
        }
    }
    var index = SearchCore.prepare(records);
    index.setVectors({dims: dims, ids: ids, data: data});
    var times = [];
    for (var t = 0; t < 7; t++) {
        var query = new Int8Array(dims);
        for (var q = 0; q < dims; q++) {
            query[q] = Math.round((random() - 0.5) * 30);
        }
        var start = now();
        index.cosineStats(query);
        times.push(now() - start);
    }
    var start2 = now();
    var result = index.search("picture", {vector: index.vectorAt(5)});
    var whole = now() - start2;
    console.log("    cosines of 10k vectors: median " + median(times).toFixed(1) + " ms; a " +
                "search by words and meaning " + whole.toFixed(1) + " ms");
    assert.ok(median(times) < 15, "comparing took " + median(times).toFixed(1) + " ms");
    assert.ok(result.total > 0);
});

/* ---------- Per-term coverage ---------------------------------------------- */

// A vector along one axis only (a word's own meaning, apart from the query's).
function axisVector(axis) {
    var values = [];
    for (var k = 0; k < MEANING_DIMS; k++) {
        values.push(k === axis ? 1 : 0);
    }
    return byteVector(values);
}

test("the texts a query embeds: the whole query, then each free word", function () {
    assert.deepStrictEqual(SearchCore.meaningTexts("anna turtle"), ["anna turtle", "anna",
                                                                    "turtle"]);
    // Small words are skipped, relation words need no vector of their own.
    assert.deepStrictEqual(SearchCore.meaningTexts("sunset over the sea"),
                           ["sunset over the sea", "sunset", "sea"]);
    // One free word: the query alone; filters, phrases and -words are no free words.
    assert.deepStrictEqual(SearchCore.meaningTexts("turtle"), ["turtle"]);
    assert.deepStrictEqual(SearchCore.meaningTexts("turtle year:2019 \"green sea\" -dog"),
                           ["turtle green sea"]);
    assert.deepStrictEqual(SearchCore.meaningTexts("year:2019"), []);
    assert.deepStrictEqual(SearchCore.meaningTexts("similar:4 turtle"), []);
    // The words of a clause that names someone or somewhere are a name.
    assert.strictEqual(SearchCore.meaningText("person:\"Anna Lopez\" OR anna turtle"), "turtle");
    assert.strictEqual(SearchCore.meaningText("place:lisbon OR lisbon tram ride"), "tram ride");
    assert.deepStrictEqual(SearchCore.meaningTexts("person:Anna OR anna turtle beach"),
                           ["turtle beach", "turtle", "beach"]);
});

// Pictures in a person's folder, turtles elsewhere: "anna turtle".
function annaIndex() {
    var records = [
        {i: 1, t: "Sea turtle in a tank", g: ["turtle", "tank"], f: "Anna's trip",
         dk: "2020-01-01", ds: 1},
        {i: 2, t: "Turtle painted on a garden wall", g: ["turtle", "wall"], dk: "2020-01-02",
         ds: 1},
        {i: 3, t: "Birthday mug", g: ["mug"], f: "Anna's trip", dk: "2020-01-03", ds: 1},
        {i: 4, t: "Green reptile swimming", g: ["reptile"], f: "Anna's trip", dk: "2020-01-04",
         ds: 1},
        {i: 5, t: "Painted shell pattern", g: ["pattern"], dk: "2020-01-05", ds: 1}
    ];
    return meaningIndex(records, {1: closeTo(0.9), 2: closeTo(0.88, 2), 3: closeTo(0.2, 3),
                                  4: closeTo(0.92, 4), 5: closeTo(0.85, 5)});
}

test("per-term coverage: a name is covered by its words only, never by meaning", function () {
    var index = annaIndex();
    // (The query means turtles: a first name adds next to nothing to it.)
    var words = {anna: axisVector(7), turtle: QUERY};
    var result = index.search("anna turtle", {vector: QUERY, termVectors: words});
    // The turtle in her folder; the reptile in her folder found by meaning;
    // not the painted turtle or the pattern (nothing of Anna there), nor her mug
    // (nothing of a turtle).
    assert.deepStrictEqual(result.items, [1, 4]);
    assert.deepStrictEqual(result.byMeaning, [4]);
    assert.deepStrictEqual(result.meaning.terms, [
        {text: "anna", name: true, relation: false, meaning: true, unknown: false},
        {text: "turtle", name: false, relation: false, meaning: true, unknown: false}]);
    // A function gives the vectors as well; without them the name is still
    // known by its words (only in folder names).
    function given(text) {
        return words[text] || null;
    }
    assert.deepStrictEqual(index.search("anna turtle", {vector: QUERY, termVectors: given}).items,
                           [1, 4]);
    assert.deepStrictEqual(index.search("anna turtle", {vector: QUERY}).items, [1, 4]);
    // One word: as before (the painted one is a turtle).
    assert.deepStrictEqual(index.search("turtle", {vector: QUERY, termVectors: words}).items
                           .slice().sort(), [1, 2, 4, 5]);
    // Words only: as before.
    assert.deepStrictEqual(index.search("anna turtle", {vector: QUERY, termVectors: words,
                                                        meaning: "words"}).items, [1]);
});

test("per-term coverage: a word lacking needs its own meaning", function () {
    var records = [
        {i: 1, t: "Red car", g: ["red", "car"], dk: "2020-01-01", ds: 1},
        {i: 2, t: "Blue car", g: ["blue", "car"], dk: "2020-01-02", ds: 1},
        {i: 3, t: "Crimson sedan", g: ["crimson", "sedan"], dk: "2020-01-03", ds: 1}
    ];
    var index = meaningIndex(records, {1: closeTo(0.9, 3), 2: closeTo(0.88, 2),
                                       3: closeTo(0.8, 3)});
    var words = {red: axisVector(3), car: QUERY};
    var result = index.search("red car", {vector: QUERY, termVectors: words});
    // The crimson sedan (a car by its words) is red by meaning; the blue
    // car is not.
    assert.deepStrictEqual(result.items, [1, 3]);
    assert.deepStrictEqual(result.byMeaning, [3]);
    // Without the words' vectors: as before (the whole query's meaning).
    assert.deepStrictEqual(index.search("red car", {vector: QUERY}).items, [1, 2, 3]);
    // A vector of another size counts as none.
    assert.deepStrictEqual(index.search("red car", {vector: QUERY, termVectors: {
        red: new Int8Array(3), car: QUERY}}).items, [1, 2, 3]);
});

test("per-term coverage: words of position need no covering", function () {
    var records = [
        {i: 1, t: "Sunset at sea", g: ["sunset", "sea"], dk: "2020-01-01", ds: 1},
        {i: 2, t: "Sunset behind the hills", g: ["sunset", "hills"], dk: "2020-01-02", ds: 1}
    ];
    var index = meaningIndex(records, {1: closeTo(0.9, 4), 2: closeTo(0.85, 2)});
    var words = {sunset: QUERY, sea: axisVector(4)};
    var result = index.search("sunset over the sea", {vector: QUERY, termVectors: words});
    // "over" is in no description: the sunset at sea is found by meaning;
    // the hills are no sea.
    assert.deepStrictEqual(result.items, [1]);
    assert.deepStrictEqual(result.byMeaning, [1]);
    assert.deepStrictEqual(result.meaning.terms.map(function (term) {
        return term.text + (term.relation ? " (position)" : "");
    }), ["sunset", "over (position)", "sea"]);
});

test("a name is a name by where it is: folders, file names, text, places, dates", function () {
    // A word in the descriptions of most pictures with it is no name, even
    // when a folder has it too.
    var records = [
        {i: 1, t: "Cactus garden", g: ["cactus"], f: "Cactus", dk: "2020-01-01", ds: 1},
        {i: 2, t: "Cactus flower", g: ["cactus"], dk: "2020-01-02", ds: 1},
        {i: 3, t: "Pot", f: "Cactus", dk: "2020-01-03", ds: 1},
        {i: 4, t: "Desert plant", g: ["plant"], dk: "2020-01-04", ds: 1},
        {i: 5, t: "Kitchen", n: "brunoway_01.jpg", dk: "2020-01-05", ds: 1},
        {i: 6, t: "Cactus with a sign", g: ["cactus"], o: "Brunoway", dk: "2020-01-06", ds: 1}
    ];
    var index = meaningIndex(records, {1: closeTo(0.9), 2: closeTo(0.88, 2), 3: closeTo(0.2, 3),
                                       4: closeTo(0.92, 4), 5: closeTo(0.1, 5),
                                       6: closeTo(0.5, 5)});
    var result = index.search("cactus brunoway", {vector: QUERY,
                                                  termVectors: {cactus: QUERY,
                                                                brunoway: axisVector(7)}});
    var names = {};
    result.meaning.terms.forEach(function (term) {
        names[term.text] = term.name;
    });
    assert.deepStrictEqual(names, {cactus: false, brunoway: true});
    // (Picture 6 has both: brunoway in its text.)
    assert.deepStrictEqual(result.items, [6]);
});

test("a smart album's search goes by the vectors of its words too", function () {
    var index = annaIndex();
    var words = {"anna turtle": QUERY, anna: axisVector(7), turtle: QUERY};
    var asked = [];
    var albums = [{id: 1, name: "Anna's turtles", kind: "smart", query: "anna turtle"}];
    var result = index.search("album:\"Anna's turtles\"", {albums: albums,
                                                             vectorFor: function (text) {
                                                                 asked.push(text);
                                                                 return words[text] || null;
                                                             }});
    assert.deepStrictEqual(asked, ["anna turtle", "anna", "turtle"]);
    assert.deepStrictEqual(result.items.slice().sort(), [1, 4]);
    // Until all of them have come, by its words alone.
    var partial = index.search("album:\"Anna's turtles\"", {albums: albums,
                                                              vectorFor: function (text) {
                                                                  return text === "anna" ? null
                                                                      : words[text];
                                                              }});
    assert.deepStrictEqual(partial.items, [1]);
});

/* ---------- Plain language (interpret) ------------------------------------- */

var PEOPLE = [{id: 1, name: "Anna Lopez", label: "Anna Lopez", ids: [1, 2]},
              {id: 2, name: "", label: "Person 2", ids: [3]},
              {id: 3, name: "Bruno", label: "Bruno", ids: [4], hidden: true},
              {id: 4, name: "Siena", label: "Siena", ids: [5]},
              {id: 5, name: "Rose", label: "Rose", ids: [6]}];

function plainRecords() {
    var records = [
        {i: 1, t: "Anna at the beach", g: ["beach"], dk: "2024-11-03", ci: "Lisbon",
         rg: "Lisbon District", co: "Portugal", cc: "PT", ds: 1},
        {i: 2, t: "Anna with a tram", g: ["tram"], dk: "2019-06-20", ci: "Lisbon",
         rg: "Lisbon District", co: "Portugal", cc: "PT", ds: 1},
        {i: 3, t: "Beach umbrellas", g: ["beach"], dk: "2024-11-05", f: "Anna Lopez", ds: 1},
        {i: 4, t: "Old bridge", g: ["bridge"], dk: "2016-04-10", ci: "Siena", rg: "Tuscany",
         co: "Italy", cc: "IT", ds: 1},
        {i: 5, t: "Dinner", g: ["dinner"], dk: "2026-07-14", ci: "Port Ellis",
         rg: "California", co: "United States", cc: "US", ds: 1},
        {i: 6, t: "Garden", g: ["garden"], dk: "2015-12-24", ds: 1}
    ];
    for (var k = 0; k < 6; k++) {
        records.push({i: 20 + k, t: "Rose " + k, g: ["rose", "flower"], dk: "2025-08-0" + (k + 1),
                      ds: 1});
    }
    return records;
}

function understood(index, text, options) {
    var opts = {people: PEOPLE, today: "2026-10-13"};
    Object.keys(options || {}).forEach(function (key) {
        opts[key] = options[key];
    });
    return index.interpret(text, opts);
}

test("plain language: a person, a place and a date become filters", function () {
    var index = SearchCore.prepare(plainRecords());
    var answer = understood(index, "anna at the beach in lisbon november 2024");
    assert.strictEqual(answer.query, "person:\"Anna Lopez\" OR anna at the beach " +
                       "place:lisbon OR lisbon date:2024-11");
    assert.deepStrictEqual(answer.parts.map(function (part) {
        return [part.kind, part.label, part.text, part.key];
    }), [["person", "Person: Anna Lopez", "anna", "anna"],
         ["place", "Place: Lisbon", "in lisbon", "lisbon"],
         ["date", "Taken: November 2024", "november 2024", "november 2024"]]);
    assert.strictEqual(answer.rest, "at the beach");
    assert.ok(answer.changed);
    assert.deepStrictEqual(answer.choices, []);
    // Her pictures and those with the name, on the beach, in Lisbon, then.
    assert.deepStrictEqual(index.search(answer.query).items, [1]);
    // What it became is understood as it is (a smart album saves it so).
    var again = understood(index, answer.query);
    assert.strictEqual(again.query, answer.query);
    assert.deepStrictEqual([again.parts, again.choices], [[], []]);
    // The search box text as typed is never changed: start and end say
    // where each part is.
    var part = answer.parts[1];
    assert.strictEqual(answer.text.slice(part.start, part.end), "in lisbon");
    // Her last name, her name with 's, and her whole name.
    assert.strictEqual(understood(index, "lopez").query, "person:\"Anna Lopez\" OR lopez");
    assert.strictEqual(understood(index, "anna's beach").query,
                       "person:\"Anna Lopez\" OR anna beach");
    assert.strictEqual(understood(index, "Anna Lopez beach").query,
                       "person:\"Anna Lopez\" OR \"anna lopez\" beach");
    // Her pictures rank with the strongest word matches, before a folder
    // that only has her name.
    assert.deepStrictEqual(index.search(understood(index, "anna beach").query).items, [1, 3]);
});

test("plain language: what is never interpreted", function () {
    var index = SearchCore.prepare(plainRecords());
    ["anna OR beach", "anna or beach", "-anna beach", "\"anna\" beach", "tag:anna",
     "person:Anna", "beach 1984", "may", "summer beach", "Person 2", "bruno"]
        .forEach(function (text) {
            var answer = understood(index, text);
            assert.strictEqual(answer.query, text, text);
            assert.deepStrictEqual(answer.parts, [], text);
        });
    // Without people (visitors, an export without names): no person.
    assert.strictEqual(index.interpret("anna beach", {today: "2026-10-13"}).query, "anna beach");
    // Turned off, or everything as typed.
    assert.strictEqual(understood(index, "anna in lisbon", {plain: false}).query,
                       "anna in lisbon");
    assert.strictEqual(understood(index, "anna in lisbon", {typed: "*"}).query, "anna in lisbon");
    // One part as typed (a chip's x): it says so in kept.
    var kept = understood(index, "anna in lisbon", {typed: ["anna"]});
    assert.strictEqual(kept.query, "anna place:lisbon OR lisbon");
    assert.deepStrictEqual(kept.kept, ["anna"]);
    assert.deepStrictEqual(understood(index, "beach", {typed: ["anna"]}).kept, []);
    // A date as typed stays whole (not "november 2024" after all).
    var month = understood(index, "beach in november 2024", {typed: ["in november 2024"]});
    assert.strictEqual(month.query, "beach in november 2024");
    assert.deepStrictEqual(month.kept, ["in november 2024"]);
    assert.strictEqual(understood(index, "beach in november 2024").parts[0].key,
                       "in november 2024");
    // Never throws.
    [null, undefined, "", "   ", "\"", "-", "OR", "in", "since", "last", "between 2015 and",
     new Array(400).join("anna ")].forEach(function (text) {
        assert.strictEqual(typeof understood(index, text).query, "string");
    });
});

test("plain language: dates in words, relative to today", function () {
    var index = SearchCore.prepare(plainRecords());
    function query(text, today) {
        return understood(index, text, today ? {today: today} : {}).query;
    }
    // (A year alone: taken then, or with the year in its words -- a folder
    // "2019 Taxes" of scans whose only date is the file's.)
    assert.strictEqual(query("beach 2019"), "beach date:2019 OR 2019");
    assert.strictEqual(query("in 2019"), "date:2019 OR 2019");
    assert.strictEqual(query("beach 2018"), "beach 2018");    // (no picture then)
    assert.strictEqual(query("2015-2019"), "date:2015..2019");
    assert.strictEqual(query("between 2019 and 2015"), "date:2015..2019");
    assert.strictEqual(query("from 2015 to 2019"), "date:2015..2019");
    assert.strictEqual(query("2015 to 2019"), "date:2015..2019");
    assert.strictEqual(query("may 2019"), "date:2019-05");
    assert.strictEqual(query("november of 2024"), "date:2024-11");
    assert.strictEqual(query("in the spring of 2016"), "date:2016-03..2016-05");
    assert.strictEqual(query("summer 2019"), "date:2019-06..2019-08");
    assert.strictEqual(query("fall 2019"), "date:2019-09..2019-11");
    // (The winter that ends in that year.)
    assert.strictEqual(query("winter 2016"), "date:2015-12..2016-02");
    assert.strictEqual(query("since 2019"), "date:2019..");
    assert.strictEqual(query("after 2019"), "date:2020..");
    assert.strictEqual(query("after march 2019"), "date:2019-04..");
    assert.strictEqual(query("before 2019"), "date:..2018");
    assert.strictEqual(query("before january 2019"), "date:..2018-12");
    assert.strictEqual(query("last year"), "date:2025");
    assert.strictEqual(query("this year"), "date:2026");
    assert.strictEqual(query("last month"), "date:2026-09");
    assert.strictEqual(query("this month"), "date:2026-10");
    assert.strictEqual(query("last month", "2026-01-15"), "date:2025-12");
    // (Weeks from Monday; 2026-10-13 is a Tuesday.)
    assert.strictEqual(query("last week"), "date:2026-10-05..2026-10-11");
    assert.strictEqual(query("this week"), "date:2026-10-12..2026-10-13");
    assert.strictEqual(query("yesterday"), "date:2026-10-12");
    // The latest one that is over.
    assert.strictEqual(query("last november"), "date:2025-11");
    assert.strictEqual(query("last september"), "date:2026-09");
    assert.strictEqual(query("last october"), "date:2025-10");
    assert.strictEqual(query("last summer"), "date:2026-06..2026-08");
    assert.strictEqual(query("last summer", "2026-07-01"), "date:2025-06..2025-08");
    assert.strictEqual(query("this summer"), "date:2026-06..2026-08");
    assert.strictEqual(query("this winter"), "date:2025-12..2026-02");
    assert.strictEqual(query("this winter", "2026-12-20"), "date:2026-12..2027-02");
    assert.strictEqual(query("last winter", "2026-01-20"), "date:2024-12..2025-02");
    // The chip says the dates.
    assert.strictEqual(understood(index, "last summer").parts[0].label,
                       "Taken: June \u2013 August 2026");
    assert.strictEqual(understood(index, "since 2019").parts[0].label, "Taken: since 2019");
    assert.strictEqual(understood(index, "before 2019").parts[0].label, "Taken: until 2018");
});

test("date: finds the pictures taken then, from one date to another", function () {
    var records = [
        {i: 1, dk: "2019-05-31"}, {i: 2, dk: "2019-06-01"}, {i: 3, dk: "2019-08-31"},
        {i: 4, dk: "2019-09-01"}, {i: 5, dk: "2019-07-04", du: 1}, {i: 6}
    ];
    function ids(query) {
        return items(records, query, {sort: "oldest"});
    }
    assert.deepStrictEqual(ids("date:2019-06..2019-08"), [2, 3]);
    assert.deepStrictEqual(ids("date:2019"), [1, 2, 3, 4]);
    assert.deepStrictEqual(ids("date:2019-06"), [2]);
    assert.deepStrictEqual(ids("date:2019-06.."), [2, 3, 4]);
    assert.deepStrictEqual(ids("date:..2019-05"), [1]);
    assert.deepStrictEqual(ids("taken:2019-05-31"), [1]);
    // (A file's time is no date taken: date: leaves it out, as on:.)
    assert.deepStrictEqual(ids("-date:2019-06..2019-08"), [1, 5, 4, 6]);
    ["date:2019-13", "date:soon", "date:2019..2018", "date:.."].forEach(function (query) {
        var result = search(records, query);
        assert.strictEqual(result.total, 0, query);
        assert.ok(/date:.* is not understood/.test(result.warnings.join(" ")), query);
    });
});

test("plain language: a word that may be several things is asked about", function () {
    var index = SearchCore.prepare(plainRecords());
    // A person and a town of the same name.
    var answer = understood(index, "siena bridge");
    assert.strictEqual(answer.query, "siena bridge");
    assert.deepStrictEqual(answer.parts, []);
    assert.strictEqual(answer.choices.length, 1);
    var choice = answer.choices[0];
    assert.deepStrictEqual([choice.text, choice.key, choice.start, choice.end],
                           ["siena", "siena", 0, 5]);
    assert.deepStrictEqual(choice.options.map(function (option) {
        return [option.kind, option.label, option.query];
    }), [["person", "Person: Siena", "person:Siena OR siena"],
         ["place", "Place: Siena", "place:siena OR siena"],
         ["words", "the word \u201csiena\u201d", null]]);
    // "in" makes it the place, "with" the person.
    assert.strictEqual(understood(index, "bridge in siena").query,
                       "bridge place:siena OR siena");
    assert.strictEqual(understood(index, "dinner with siena").query,
                       "dinner person:Siena OR siena");
    // A name that is also a common word of the pictures (six roses).
    var rose = understood(index, "rose");
    assert.strictEqual(rose.query, "rose");
    assert.deepStrictEqual(rose.choices[0].options.map(function (option) {
        return option.kind;
    }), ["person", "words"]);
    assert.strictEqual(understood(index, "with rose").query, "person:Rose OR rose");
    // Kept as typed: not asked again.
    assert.deepStrictEqual(understood(index, "rose", {typed: ["rose"]}).choices, []);
});

test("plain language: places of the records, other names of countries", function () {
    var index = SearchCore.prepare(plainRecords());
    assert.strictEqual(understood(index, "dinner in port ellis").query,
                       "dinner place:\"port ellis\" OR \"port ellis\"");
    assert.strictEqual(understood(index, "tuscany").parts[0].label, "Place: Tuscany");
    var usa = understood(index, "usa dinner");
    assert.strictEqual(usa.query, "place:usa OR usa dinner");
    assert.strictEqual(usa.parts[0].label, "Place: United States");
    assert.deepStrictEqual(index.search(usa.query).items, [5]);
    // A place no picture was taken in is a word.
    assert.strictEqual(understood(index, "paris").query, "paris");
});

test("plain language in an export: published names are looked for as words", function () {
    var index = SearchCore.prepare(plainRecords());
    var answer = index.interpret("anna beach", {names: ["Anna Lopez"], today: "2026-10-13"});
    assert.strictEqual(answer.query, "\"anna\" beach");
    assert.strictEqual(answer.parts[0].label, "Person: Anna Lopez");
    // (Quoted: never found by meaning.)
    assert.strictEqual(SearchCore.meaningText(answer.query), "anna beach");
    assert.deepStrictEqual(SearchCore.meaningTexts(answer.query), ["anna beach"]);
});

/* An export with its "people_names" option (export.add_people_names): Anna
   is a tag of her pictures (lower case), six at the beach and three with
   turtles; a turtle mosaic with nothing of her is as close to "anna turtle"
   as her turtles are. */
function publishedIndex() {
    var records = [];
    var vectors = {};
    for (var k = 1; k <= 6; k++) {
        records.push({i: k, t: "Beach day " + k, g: ["beach", "anna"], dk: "2024-06-0" + k,
                      ds: 1});
        vectors[k] = closeTo(0.2, 3);
    }
    [0.9, 0.88, 0.86].forEach(function (cos, k) {
        records.push({i: 7 + k, t: "Sea turtle in a tank " + k, g: ["turtle", "tank", "anna"],
                      dk: "2024-07-0" + (k + 1), ds: 1});
        vectors[7 + k] = closeTo(cos, 2);
    });
    records.push({i: 10, t: "Sea turtle mosaic on a tiled floor", g: ["turtle", "mosaic"],
                  dk: "2024-08-01", ds: 1});
    vectors[10] = closeTo(0.87, 4);
    return meaningIndex(records, vectors);
}

test("plain language in an export: a published name is a name, though it is a tag",
     function () {
         // (The names are tags of five or more pictures: not a common word.)
         var index = publishedIndex();
         var answer = index.interpret("anna turtle", {names: ["Anna"], today: "2026-10-13"});
         assert.deepStrictEqual(answer.choices, []);
         assert.strictEqual(answer.query, "\"anna\" turtle");
         assert.strictEqual(answer.parts[0].label, "Person: Anna");
         // Shown as understood: the mosaic has nothing of Anna.
         var words = {anna: axisVector(7), turtle: QUERY};
         var options = {vector: QUERY, termVectors: words, names: ["Anna"]};
         assert.deepStrictEqual(index.search(answer.query, options).items.slice().sort(),
                                [7, 8, 9]);
         // Searched as typed (the words alone, "Search the words as typed"): the
         // name is covered by its words only, never by meaning.
         var typed = index.search("anna turtle", options);
         assert.deepStrictEqual(typed.items.slice().sort(), [7, 8, 9]);
         assert.deepStrictEqual(typed.meaning.terms.map(function (term) {
             return [term.text, term.name];
         }), [["anna", true], ["turtle", false]]);
         // (A library's own tag is what its pictures show: the same records
         // without published names find the mosaic by meaning.)
         assert.ok(index.search("anna turtle", {vector: QUERY, termVectors: words}).items
                   .indexOf(10) >= 0);
         // A smart album of the export searches the same way.
         var albums = [{id: 1, name: "Turtles", kind: "smart", query: "anna turtle"}];
         var album = index.search("album:Turtles", {albums: albums, names: ["Anna"],
                                                    vectorFor: function (text) {
                                                        return text === "anna turtle" ? QUERY
                                                            : words[text] || null;
                                                    }});
         assert.deepStrictEqual(album.items.slice().sort(), [7, 8, 9]);
     });

test("plain language in an export: a published name that is a common word is asked about",
     function () {
         // Rose, a published name, and six roses whose titles say so.
         var records = [{i: 1, t: "Garden party", g: ["garden", "rose"], dk: "2024-06-01",
                         ds: 1}];
         for (var k = 0; k < 6; k++) {
             records.push({i: 2 + k, t: "Red rose " + k, g: ["rose", "flower"],
                           dk: "2024-06-0" + (k + 2), ds: 1});
         }
         var index = SearchCore.prepare(records);
         var answer = index.interpret("rose", {names: ["Rose"], today: "2026-10-13"});
         assert.strictEqual(answer.choices.length, 1);
         assert.deepStrictEqual(answer.choices[0].options.map(function (option) {
             return option.kind;
         }), ["person", "words"]);
     });

/* ---------- Plain language and per-term coverage: review fixes ------------ */

test("per-term coverage: a word next to the others, judged by the pictures with them all",
     function () {
         // "red sports car": a red coupe says "car" and "red", not "sports";
         // the word's own vector means sports games, not a car.
         function cars(full) {
             var records = [];
             var vectors = {};
             if (full) {
                 [0.9, 0.88, 0.86].forEach(function (cos, k) {
                     records.push({i: 1 + k, t: "Red sports car " + k, g: ["red", "sports", "car"],
                                   dk: "2020-01-01", ds: 1});
                     vectors[1 + k] = closeTo(cos, 2);
                 });
             }
             records.push({i: 4, t: "Red coupe", g: ["red", "car", "coupe"], dk: "2020-01-02",
                           ds: 1});
             vectors[4] = closeTo(0.85, 2);
             records.push({i: 5, t: "Red car in a garage", g: ["red", "car", "garage"],
                           dk: "2020-01-03", ds: 1});
             vectors[5] = closeTo(0.66, 4);
             records.push({i: 6, t: "Sports day on a field", g: ["sports", "field"],
                           dk: "2020-01-04", ds: 1});
             vectors[6] = closeTo(0.8, 5);
             return meaningIndex(records, vectors, 600);
         }
         var words = {red: axisVector(5), sports: axisVector(3), car: axisVector(2)};
         var options = {vector: QUERY, termVectors: words};
         // The coupe is as close to the whole query as the sports cars: it
         // stays; the car in the garage is not, and the field has no car.
         var result = cars(true).search("red sports car", options);
         assert.deepStrictEqual(result.items, [1, 2, 3, 4]);
         assert.deepStrictEqual(result.byMeaning, [4]);
         // With no picture of all the words, each part must be seen: "sports"
         // by its own meaning, which the coupe does not have.
         assert.deepStrictEqual(cars(false).search("red sports car", options).items, []);
     });

test("per-term coverage: a word no picture has (a misspelling) is not checked", function () {
    var records = [
        {i: 1, t: "Waterfall on a hike", g: ["waterfall", "hike", "trail"], dk: "2020-01-01", ds: 1},
        {i: 2, t: "Waterfront path", g: ["waterfront", "path"], dk: "2020-01-02", ds: 1}
    ];
    var index = meaningIndex(records, {1: closeTo(0.9, 2), 2: closeTo(0.5, 3)});
    // (The misspelling's own vector is spelling noise: like the waterfront.)
    var result = index.search("waterfal hike", {vector: QUERY,
                                                termVectors: {waterfal: axisVector(3),
                                                              hike: QUERY}});
    assert.deepStrictEqual(result.items, [1]);
    assert.deepStrictEqual(result.meaning.terms.map(function (term) {
        return [term.text, term.unknown];
    }), [["waterfal", true], ["hike", false]]);
});

test("per-term coverage: a word whose pictures stand far out needs more than z 2", function () {
    // Five puppies (their own word's vector finds them far out); a snowy
    // yard that only leans a little towards puppies is no puppy in the snow.
    var records = [];
    var vectors = {};
    for (var k = 0; k < 5; k++) {
        records.push({i: 1 + k, t: "Puppy " + k, g: ["puppy"], dk: "2020-01-01", ds: 1});
        vectors[1 + k] = byteVector([0.3, 0, 0, 0.95, 0, 0, 0, 0]);
    }
    records.push({i: 9, t: "Snowy yard", g: ["snow", "yard"], dk: "2020-02-01", ds: 1});
    vectors[9] = byteVector([0.9, 0, 0, 0.4, 0, 0, 0, 0]);
    var index = meaningIndex(records, vectors);
    var puppy = index.cosineStats(axisVector(3));
    function z(id) {
        var r = index.position[String(id)];
        return (puppy.values[r] - puppy.mean) / puppy.std;
    }
    // (The yard is above z 2 for "puppy", far below the puppies.)
    assert.ok(z(9) > 2 && z(9) < z(1) - 2.5, z(9) + " " + z(1));
    var result = index.search("puppy snow", {vector: QUERY,
                                             termVectors: {puppy: axisVector(3), snow: QUERY}});
    assert.deepStrictEqual(result.items, []);
});

test("per-term coverage: not in a small set, where the whole query decides", function () {
    // Nineteen pictures, as an export of a small library has. The coffee
    // cups have neither "warm" nor "drink" in their words, and stand a
    // little above the rest for the whole query (z 2.7), as the closest few
    // of a small set do; on its own, "warm" (a word of another picture)
    // puts them below the mean. In a library of hundreds of pictures cups
    // even far above the rest for the whole query need each word's own
    // meaning, and "warm" lacks.
    function cups(fillers) {
        var records = [
            {i: 1, t: "Coffee cups on a cafe table", g: ["coffee", "cups", "table"],
             dk: "2020-01-01", ds: 1},
            {i: 2, t: "Warm light on a wall", g: ["warm", "light", "wall"], dk: "2020-01-02",
             ds: 1}
        ];
        var vectors = {1: closeTo(fillers ? 0.9 : 0.5, 3), 2: closeTo(0.15, 4)};
        if (fillers) {
            return meaningIndex(records, vectors, fillers);
        }
        for (var k = 0; k < 17; k++) {
            records.push({i: 100 + k, t: "Picture " + k, dk: "2020-02-01", ds: 1});
            vectors[100 + k] = closeTo(0.05 + 0.3 * k / 16, 1 + (k % 2) * 4);
        }
        var ids = Object.keys(vectors).map(Number);
        var data = new Int8Array(ids.length * MEANING_DIMS);
        ids.forEach(function (id, n) {
            data.set(vectors[id], n * MEANING_DIMS);
        });
        var index = SearchCore.prepare(records);
        index.setVectors({dims: MEANING_DIMS, ids: ids, data: data});
        return index;
    }
    var words = {warm: axisVector(4), drink: axisVector(3)};
    var small = cups(0);
    assert.strictEqual(small.vecCount, 19);
    var whole = small.cosineStats(QUERY);
    var at = small.position["1"];
    var z = (whole.values[at] - whole.mean) / whole.std;
    assert.ok(z > 2.2 && z < 3, String(z));
    var warm = small.cosineStats(axisVector(4));
    assert.ok((warm.values[at] - warm.mean) / warm.std < 0, "warm");
    var result = small.search("warm drink", {vector: QUERY, termVectors: words});
    assert.deepStrictEqual(result.items, [1]);
    assert.deepStrictEqual(result.byMeaning, [1]);
    // (No term was judged on its own.)
    assert.deepStrictEqual(result.meaning.terms, []);
    // A library of hundreds: "warm" is not covered.
    var large = cups(400);
    assert.deepStrictEqual(large.search("warm drink", {vector: QUERY, termVectors: words}).items,
                           []);
    assert.deepStrictEqual(large.search("warm drink", {vector: QUERY}).items, [1]);
});

test("per-term coverage: a folder's word that pictures show is no name", function () {
    // Ten pictures in "Wood Carving" (one says "carving"), all much like
    // the word; a carved wooden sculpture elsewhere is close to it too.
    var records = [];
    var vectors = {};
    for (var k = 0; k < 10; k++) {
        records.push({i: 1 + k, t: "Woodcarver at work " + k, g: k ? ["wood", "woodcarver"]
                      : ["wood", "carving"], f: "Wood Carving", dk: "2020-01-01", ds: 1});
        vectors[1 + k] = closeTo(0.95, 2);
    }
    records.push({i: 20, t: "Carved wooden sculpture", g: ["wood", "sculpture"], dk: "2020-01-02",
                  ds: 1});
    vectors[20] = closeTo(0.78, 3);
    var index = meaningIndex(records, vectors, 1000);
    var result = index.search("wood carving", {vector: QUERY,
                                               termVectors: {wood: axisVector(4),
                                                             carving: QUERY}});
    var named = {};
    result.meaning.terms.forEach(function (term) {
        named[term.text] = term.name;
    });
    assert.deepStrictEqual(named, {wood: false, carving: false});
    assert.ok(result.items.indexOf(20) >= 0, JSON.stringify(result.items));
    assert.deepStrictEqual(result.byMeaning, [20]);
});

test("the texts a query embeds are as many as /api/embed takes at most", function () {
    var texts = SearchCore.meaningTexts("red car blue truck green bus yellow bike old house " +
                                        "white barn");
    assert.strictEqual(SearchCore.MEANING_TEXTS, 9);
    assert.strictEqual(texts.length, SearchCore.MEANING_TEXTS);
    assert.deepStrictEqual(texts.slice(0, 3), ["red car blue truck green bus yellow bike old " +
                                               "house white barn", "red", "car"]);
});

test("plain language: a year typed is the date taken or the year in the words", function () {
    var records = [
        {i: 1, t: "Christmas tree", g: ["christmas", "tree"], dk: "2011-12-24", ds: 1},
        {i: 2, t: "Christmas card scan", g: ["christmas", "card"], f: "Christmas 2011", ds: 1},
        {i: 3, t: "Christmas lights", g: ["christmas", "lights"], f: "Christmas 2011",
         dk: "2019-03-01", du: 1, ds: 1},
        {i: 4, t: "Holiday dinner", g: ["dinner"], dk: "2011-12-20", ds: 1},
        {i: 5, t: "Christmas morning", g: ["christmas"], dk: "2015-12-25", ds: 1}
    ];
    var index = SearchCore.prepare(records);
    var answer = index.interpret("christmas 2011", {today: "2026-10-13"});
    assert.strictEqual(answer.query, "christmas date:2011 OR 2011");
    assert.deepStrictEqual(index.search(answer.query, {sort: "oldest"}).items.slice().sort(),
                           [1, 2, 3]);
    // (Only a year typed: "last year" is the date alone.)
    assert.strictEqual(index.interpret("christmas last year", {today: "2012-06-01"}).query,
                       "christmas date:2011");
    // The words of a date are no words meaning looks for.
    assert.strictEqual(SearchCore.meaningText(answer.query), "christmas");
});

test("plain language: since, after and before a relative date; years with a dash", function () {
    var index = SearchCore.prepare(plainRecords());
    function query(text) {
        return understood(index, text).query;
    }
    assert.strictEqual(query("since last year"), "date:2025..");
    assert.strictEqual(query("beach after last year"), "beach date:2026..");
    assert.strictEqual(query("snow since last summer"), "snow date:2026-06..");
    assert.strictEqual(query("beach before this year"), "beach date:..2025");
    assert.strictEqual(query("beach since yesterday"), "beach date:2026-10-12..");
    assert.strictEqual(query("after last month"), "date:2026-10..");
    assert.strictEqual(query("before last week"), "date:..2026-10-04");
    assert.strictEqual(understood(index, "since last year").parts[0].label, "Taken: since 2025");
    // "2015 - 2019" typed with spaces, as "2015-2019".
    assert.strictEqual(query("2015 - 2019 beach"), "date:2015..2019 beach");
    assert.strictEqual(understood(index, "2015 - 2019 beach").rest, "beach");
});

test("plain language: words about taking the picture are left out", function () {
    var index = SearchCore.prepare(plainRecords());
    function check(text, query, dropped) {
        var answer = understood(index, text);
        assert.strictEqual(answer.query, query, text);
        assert.deepStrictEqual(answer.dropped, dropped, text);
        return answer;
    }
    // With a place, a date or a person understood.
    var italy = check("taken in Italy", "place:italy OR italy", ["taken"]);
    assert.deepStrictEqual(italy.parts.map(function (part) {
        return part.label;
    }), ["Place: Italy"]);
    assert.strictEqual(italy.rest, "");
    assert.deepStrictEqual(index.search(italy.query).items, [4]);
    var lisbon = check("pictures taken in Lisbon", "pictures place:lisbon OR lisbon", ["taken"]);
    assert.deepStrictEqual(index.search(lisbon.query).items.slice().sort(), [1, 2]);
    check("something in Italy", "place:italy OR italy", ["something"]);
    check("photographed in november 2024", "date:2024-11", ["photographed"]);
    check("bridge shot in Italy", "bridge place:italy OR italy", ["shot"]);
    // Before "in", "at", ... and more words: a place known only by the
    // words of its pictures, or no place at all.
    var beach = check("taken at the beach", "at the beach", ["taken"]);
    assert.deepStrictEqual(index.search(beach.query).items.slice().sort(), [1, 3]);
    check("Something on the bridge", "on the bridge", ["Something"]);
    // Otherwise they stay words.
    check("something warm to drink", "something warm to drink", []);
    check("shot glass", "shot glass", []);
    check("taken in", "taken in", []);
    check("\"taken in\" lisbon", "\"taken in\" place:lisbon OR lisbon", []);
    // As typed: everything stays.
    var typed = understood(index, "taken in Italy", {typed: "*"});
    assert.deepStrictEqual([typed.query, typed.dropped], ["taken in Italy", []]);
    // Only the place as typed: "taken" still goes before "in" and more words.
    var kept = understood(index, "taken in Italy", {typed: ["italy"]});
    assert.deepStrictEqual([kept.query, kept.dropped, kept.kept], ["in Italy", ["taken"],
                                                                  ["italy"]]);
});

test("plain language: us is a small word, US the country", function () {
    var records = plainRecords();
    records.push({i: 30, t: "Beach day", g: ["beach"], dk: "2025-08-10", ds: 1});
    var index = SearchCore.prepare(records);
    ["pictures of us at the beach", "show us the beach", "kids with us"].forEach(function (text) {
        var answer = understood(index, text);
        assert.strictEqual(answer.query, text, text);
        assert.deepStrictEqual(answer.parts, [], text);
    });
    var us = understood(index, "dinner in US");
    assert.strictEqual(us.query, "dinner place:us OR us");
    assert.strictEqual(us.parts[0].label, "Place: United States");
});

test("plain language: a place named like what pictures show is asked about", function () {
    var records = [];
    for (var k = 0; k < 6; k++) {
        records.push({i: 10 + k, t: "Sunset over the Atlantic Ocean " + k,
                      g: ["sunset", "ocean", "atlantic"], dk: "2021-01-0" + (k + 1), ds: 1});
    }
    for (k = 0; k < 4; k++) {
        records.push({i: 1 + k, t: "Street " + k, g: ["street"], dk: "2020-01-0" + (k + 1),
                      ci: "Atlantic", rg: "Iowa", co: "United States", cc: "US", ds: 1});
        records.push({i: 20 + k, t: "Lake view " + k, g: ["lake"], dk: "2022-01-0" + (k + 1),
                      ci: "Fuji", rg: "Shizuoka", co: "Japan", cc: "JP", ds: 1});
        records.push({i: 30 + k, t: "Pier " + k, g: ["pier"], dk: "2023-01-0" + (k + 1),
                      ci: "Red Hill", rg: "South Australia", co: "Australia", cc: "AU", ds: 1});
    }
    for (k = 0; k < 6; k++) {
        records.push({i: 40 + k, t: "Red hill above a beach " + k, g: ["red", "hill", "beach"],
                      dk: "2024-01-0" + (k + 1), ds: 1});
    }
    var index = SearchCore.prepare(records);
    function asked(text, wanted) {
        var answer = index.interpret(text, {today: "2026-10-13"});
        assert.strictEqual(answer.query, text, text);
        assert.deepStrictEqual(answer.choices.map(function (choice) {
            return choice.text;
        }), [wanted], text);
        assert.deepStrictEqual(answer.choices[0].options.map(function (option) {
            return option.kind;
        }), ["place", "words"], text);
    }
    // As many pictures say "atlantic" as were taken in the town.
    asked("sunset over the atlantic", "atlantic");
    asked("atlantic ocean waves", "atlantic");
    // Both words are what pictures show.
    asked("red hill beach", "red hill");
    // "Mount" before it: the mountain more likely than the town.
    asked("mount fuji", "fuji");
    // "In" makes it the place; a town no picture says is taken.
    assert.strictEqual(index.interpret("street in atlantic", {today: "2026-10-13"}).query,
                       "street place:atlantic OR atlantic");
    assert.strictEqual(index.interpret("fuji lake", {today: "2026-10-13"}).query,
                       "place:fuji OR fuji lake");
});

test("plain language: someone named like a month is asked about", function () {
    var records = plainRecords();
    records.push({i: 31, t: "Picnic", g: ["picnic"], dk: "2019-06-10", ds: 1});
    var index = SearchCore.prepare(records);
    var people = PEOPLE.concat([{id: 6, name: "June Smith", label: "June Smith", ids: [31]}]);
    var answer = understood(index, "june 2019", {people: people});
    assert.strictEqual(answer.query, "june 2019");
    assert.deepStrictEqual(answer.choices[0].options.map(function (option) {
        return [option.kind, option.label, option.query];
    }), [["date", "Taken: June 2019", "date:2019-06"],
         ["person", "Person: June Smith (2019)", "person:\"June Smith\" OR june date:2019 OR 2019"],
         ["words", "the words " + String.fromCharCode(0x201c) + "june 2019" +
          String.fromCharCode(0x201d), null]]);
    // Without a year: the person or the word, asked; "with" makes it the person.
    assert.deepStrictEqual(understood(index, "photos in june", {people: people}).choices[0]
                           .options.map(function (option) {
                               return option.kind;
                           }), ["person", "words"]);
    assert.strictEqual(understood(index, "picnic with june", {people: people}).query,
                       "picnic person:\"June Smith\" OR june");
    // Nobody called June: the month.
    assert.strictEqual(understood(index, "june 2019").query, "date:2019-06");
});

test("plain language: small parts of names are no names on their own", function () {
    var index = SearchCore.prepare(plainRecords());
    var people = [{id: 7, name: "Ana de la Cruz", label: "Ana de la Cruz", ids: [1]},
                  {id: 8, name: "Pieter van Dijk", label: "Pieter van Dijk", ids: [2]}];
    ["la beach", "dinner in la", "van at the beach"].forEach(function (text) {
        assert.strictEqual(understood(index, text, {people: people}).query, text, text);
    });
    assert.strictEqual(understood(index, "cruz beach", {people: people}).query,
                       "person:\"Ana de la Cruz\" OR cruz beach");
    assert.strictEqual(understood(index, "pieter van dijk", {people: people}).query,
                       "person:\"Pieter van Dijk\" OR \"pieter van dijk\"");
});

test("plain language: the rest of the search as typed (others)", function () {
    var index = SearchCore.prepare(plainRecords());
    var answer = understood(index, "anna tag:beach -dog");
    assert.strictEqual(answer.others, "tag:beach -dog");
    assert.strictEqual(answer.rest, "");
    answer = understood(index, "lisbon \"red car\" OR boat");
    assert.strictEqual(answer.query, "place:lisbon OR lisbon \"red car\" OR boat");
    assert.strictEqual(answer.others, "\"red car\" OR boat");
    assert.strictEqual(understood(index, "cat AND dog 2019").others, "");
});

test("a person's pictures before pictures with the name written in them", function () {
    var records = plainRecords();
    records.push({i: 32, t: "Mug with Anna on it", g: ["mug", "anna"], o: "Anna",
                  dk: "2024-01-01", ds: 1});
    var index = SearchCore.prepare(records);
    var answer = understood(index, "anna");
    var found = index.search(answer.query, {people: PEOPLE}).items;
    assert.deepStrictEqual(found.slice(0, 2).sort(), [1, 2]);
    assert.ok(found.indexOf(32) > 1, JSON.stringify(found));
});

test("the dates pictures were taken on: first and last", function () {
    var index = SearchCore.prepare(plainRecords());
    assert.deepStrictEqual(index.takenSpan(), {from: "2015-12-24", to: "2026-07-14"});
    assert.strictEqual(SearchCore.prepare([{i: 1, t: "x"}]).takenSpan(), null);
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
