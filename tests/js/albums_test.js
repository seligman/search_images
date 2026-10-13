/* Tests for favorites, ratings and albums in static/search-core.js (is:favorite,
   rating:, album:, the Favorites and Rating facets, the album order) and
   site.js (the facet groups and sorts of an address, marks in compact
   records), under Node (no packages needed).

   Run by tests/test_search_core.py, or directly:
       node tests/js/albums_test.js
   Prints one line per test and exits with status 1 when any test fails.
   All records are made up here.
*/
"use strict";

var assert = require("assert");
var path = require("path");

var STATIC = path.join(__dirname, "..", "..", "static");
var SearchCore = require(path.join(STATIC, "search-core.js"));
global.window = {location: {search: "", protocol: "http:"}};
var SITE = require(path.join(STATIC, "site.js"));

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

function records() {
    return [
        {i: 1, l: "a", t: "Beach at sunset", g: ["beach", "sunset"], dk: "2020-01-01",
         ts: "2020-01-01T10:00:00", fav: 1, rt: 5, al: [10]},
        {i: 2, l: "a", t: "Beach with a dog", g: ["beach", "dog"], dk: "2020-01-02",
         ts: "2020-01-02T10:00:00", rt: 3, al: [10, 11]},
        {i: 3, l: "a", t: "Mountain lake", g: ["lake"], dk: "2020-01-03",
         ts: "2020-01-03T10:00:00", fav: 1},
        {i: 4, l: "b", t: "Dog in snow", g: ["dog", "snow"], dk: "2020-01-04",
         ts: "2020-01-04T10:00:00", rt: 1, al: [11]},
        {i: 5, l: "b", t: "City at night", g: ["city"], dk: "2020-01-05",
         ts: "2020-01-05T10:00:00", h: 1, fav: 1, al: [10]}
    ];
}

var ALBUMS = [
    {id: 10, name: "Summer Trip", kind: "manual", cover: null, items: [5, 2, 1]},
    {id: 11, name: "Dogs", kind: "manual", cover: 4, items: [4, 2]},
    {id: 12, name: "Best beaches", kind: "smart", query: "beach",
     options: {filters: {rating: ["5", "4"]}, sort: "oldest"}},
    {id: 13, name: "Loop", kind: "smart", query: "album:Loop"},
    {id: 14, name: "Rated \"good\"", kind: "manual", items: [3]}
];

function ids(query, options) {
    var opts = {albums: ALBUMS};
    Object.keys(options || {}).forEach(function (key) {
        opts[key] = options[key];
    });
    return SearchCore.search(records(), query, opts).items;
}

test("is:favorite, is:rated and is:unrated", function () {
    assert.deepStrictEqual(ids("is:favorite").sort(), [1, 3]);
    assert.deepStrictEqual(ids("is:fav", {hidden: true}).sort(), [1, 3, 5]);
    assert.deepStrictEqual(ids("is:favourite beach"), [1]);
    assert.deepStrictEqual(ids("-is:favorite").sort(), [2, 4]);
    assert.deepStrictEqual(ids("is:rated").sort(), [1, 2, 4]);
    assert.deepStrictEqual(ids("is:unrated").sort(), [3]);
});

test("rating: exactly, at least, at most, a range, none", function () {
    var cases = {"rating:5": [1], "rating:>=3": [1, 2], "rating:>3": [1], "rating:4+": [1],
                 "rating:<=3": [2, 4], "rating:<3": [4], "rating:1-3": [2, 4],
                 "rating:3..1": [2, 4], "rating:0": [3], "rating:none": [3],
                 "stars:>=1": [1, 2, 4], "rating:=>5": [1]};
    Object.keys(cases).forEach(function (query) {
        assert.deepStrictEqual(ids(query).sort(), cases[query], query);
    });
    ["rating:6", "rating:>5", "rating:<1", "rating:good"].forEach(function (query) {
        var result = SearchCore.search(records(), query, {albums: ALBUMS});
        assert.strictEqual(result.items.length, 0, query);
        if (query !== "rating:>5" && query !== "rating:<1") {
            assert.ok(result.warnings.length, query);
        }
    });
});

test("album: finds a manual album by its name, any case, quoted or not", function () {
    assert.deepStrictEqual(ids("album:\"summer trip\"").sort(), [1, 2]);
    assert.deepStrictEqual(ids("album:\"Summer Trip\"", {hidden: true}).sort(), [1, 2, 5]);
    assert.deepStrictEqual(ids("album:dogs").sort(), [2, 4]);
    assert.deepStrictEqual(ids("album:dog").sort(), [2, 4]);      // the only one starting so
    assert.deepStrictEqual(ids("album:11").sort(), [2, 4]);       // its id
    assert.deepStrictEqual(ids("album:\"Rated good\""), [3]);     // quotes cannot be typed
    assert.deepStrictEqual(ids("album:dogs -album:\"summer trip\""), [4]);
    assert.deepStrictEqual(ids("album:dogs OR album:\"summer trip\"").sort(), [1, 2, 4]);
    var result = SearchCore.search(records(), "album:nothing", {albums: ALBUMS});
    assert.strictEqual(result.items.length, 0);
    assert.ok(/no album called "nothing"/.test(result.warnings.join(" ")), result.warnings);
    // Without the albums (an export without its marks) nothing matches.
    assert.strictEqual(SearchCore.search(records(), "album:dogs", {}).items.length, 0);
});

test("a picture put in an album on the page is in it at once", function () {
    var recs = records();
    var index = SearchCore.prepare(recs);
    recs[2].al = [11];
    assert.deepStrictEqual(index.search("album:dogs", {albums: ALBUMS}).items, [4, 2, 3]);
    recs[2].fav = 0;
    delete recs[0].fav;
    assert.deepStrictEqual(index.search("is:favorite", {albums: ALBUMS}).items, []);
});

test("one album alone shows in its order; another sort can be chosen", function () {
    var result = SearchCore.search(records(), "album:\"summer trip\"",
                                   {albums: ALBUMS, hidden: true});
    assert.deepStrictEqual(result.items, [5, 2, 1]);
    assert.strictEqual(result.sort, "album");
    assert.deepStrictEqual(result.album, {id: 10, name: "Summer Trip", kind: "manual"});
    assert.deepStrictEqual(ids("album:\"summer trip\"", {hidden: true, sort: "oldest"}),
                           [1, 2, 5]);
    // With words to rank by, the best match comes first unless album order is chosen.
    var ranked = SearchCore.search(records(), "album:\"summer trip\" sunset",
                                   {albums: ALBUMS});
    assert.strictEqual(ranked.sort, "relevance");
    assert.deepStrictEqual(ids("album:\"summer trip\" beach", {sort: "album"}), [2, 1]);
    // "album" without an album to follow: the default order.
    var plain = SearchCore.search(records(), "beach", {albums: ALBUMS, sort: "album"});
    assert.strictEqual(plain.sort, "relevance");
    assert.strictEqual(plain.album, null);
});

test("a smart album runs its saved search", function () {
    var result = SearchCore.search(records(), "album:\"best beaches\"", {albums: ALBUMS});
    assert.deepStrictEqual(result.items, [1]);
    assert.strictEqual(result.album.kind, "smart");
    // Its own sort is its order.
    var both = ALBUMS.concat([{id: 15, name: "Dog days", kind: "smart", query: "dog",
                               options: {sort: "oldest"}}]);
    assert.deepStrictEqual(SearchCore.search(records(), "album:\"dog days\"",
                                             {albums: both}).items, [2, 4]);
    // An album that names itself finds nothing, and says so.
    var loop = SearchCore.search(records(), "album:loop", {albums: ALBUMS});
    assert.deepStrictEqual(loop.items, []);
    assert.ok(/names itself/.test(loop.warnings.join(" ")), loop.warnings);
    // A smart album of albums.
    var nested = ALBUMS.concat([{id: 16, name: "Dog favorites", kind: "smart",
                                 query: "album:dogs OR is:favorite"}]);
    assert.deepStrictEqual(SearchCore.search(records(), "album:\"dog favorites\"",
                                             {albums: nested, sort: "oldest"}).items,
                           [1, 2, 3, 4]);
    // By meaning: vectorFor gives the saved search's vector (null: by its words).
    var asked = [];
    SearchCore.search(records(), "album:\"best beaches\"", {albums: ALBUMS,
        vectorFor: function (text) {
            asked.push(text);
            return null;
        }});
    assert.deepStrictEqual(asked, ["beach"]);
});

test("the Favorites and Rating facets count what the records say now", function () {
    var recs = records();
    var index = SearchCore.prepare(recs);
    var result = index.search("", {albums: ALBUMS});
    assert.deepStrictEqual(result.facets.favorite, [{value: "yes", count: 2, selected: false}]);
    assert.deepStrictEqual(result.facets.rating.map(function (entry) {
        return [entry.value, entry.count];
    }), [["5", 1], ["3", 1], ["1", 1]]);
    var chosen = index.search("", {filters: {rating: ["5", "3"]}});
    assert.deepStrictEqual(chosen.items.sort(), [1, 2]);
    // The other ratings still count (choosing one more widens the search).
    assert.deepStrictEqual(chosen.facets.rating.map(function (entry) {
        return [entry.value, entry.count, entry.selected];
    }), [["5", 1, true], ["3", 1, true], ["1", 1, false]]);
    assert.deepStrictEqual(index.search("", {filters: {favorite: ["yes"]}}).items.sort(),
                           [1, 3]);
    // Changed in place: seen at once, without preparing the records again.
    recs[3].fav = 1;
    recs[3].rt = 5;
    var again = index.search("", {filters: {favorite: ["yes"], rating: ["5"]}});
    assert.deepStrictEqual(again.items.sort(), [1, 4]);
    assert.strictEqual(SearchCore.facetLabel("rating", "1"), "1 star");
    assert.strictEqual(SearchCore.facetLabel("favorite", "yes"), "Favorites");
    // Records without marks (an export without them): no entries at all.
    var plain = SearchCore.search([{i: 1, t: "x"}], "", {});
    assert.deepStrictEqual([plain.facets.favorite, plain.facets.rating], [[], []]);
});

test("findAlbum, the facet groups of an address and the album order", function () {
    assert.strictEqual(SearchCore.findAlbum(ALBUMS, "SUMMER  trip").id, 10);
    assert.strictEqual(SearchCore.findAlbum(ALBUMS, "rated \u201cgood\u201d").id, 14);
    assert.strictEqual(SearchCore.findAlbum(ALBUMS, ""), null);
    assert.strictEqual(SearchCore.findAlbum(ALBUMS, "best").id, 12);
    assert.strictEqual(SearchCore.findAlbum(null, "x"), null);
    // site.js keeps the facet groups and sorts of SearchCore in the address.
    assert.deepStrictEqual(SITE.searchState.FACET_KEYS, SearchCore.FACETS.map(function (facet) {
        return facet.key;
    }));
    var state = SITE.searchState.read("?q=album%3Adogs&favorite=yes&rating=5&rating=4" +
                                      "&sort=album");
    assert.deepStrictEqual(state.filters, {favorite: ["yes"], rating: ["5", "4"]});
    assert.strictEqual(state.sort, "album");
    assert.strictEqual(SITE.searchState.params(state),
                       "q=album%3Adogs&favorite=yes&rating=5&rating=4&sort=album");
    assert.ok(SearchCore.SORTS.indexOf("album") >= 0);
});

test("album:#<id> names an album by its id only; albumTerm says it so", function () {
    var odd = [{id: 3, name: "Say \"hi\"", kind: "manual", items: [1]},
               {id: 7, name: "3", kind: "manual", items: [2]},
               {id: 8, name: "Caf\u00e9", kind: "manual", items: [3]},
               {id: 9, name: "Cafe", kind: "manual", items: [4]},
               {id: 10, name: "Summer Trip", kind: "manual", items: [5]}];
    // (Records without al: the albums' items say where they are.)
    var plain = records().map(function (rec) {
        delete rec.al;
        return rec;
    });
    function found(query) {
        return SearchCore.search(plain, query, {albums: odd, hidden: true}).items;
    }
    assert.deepStrictEqual(found("album:#3"), [1]);
    assert.deepStrictEqual(found("album:\"#3\""), [1]);
    assert.deepStrictEqual(found("album:3"), [2]);              // a name comes first
    assert.deepStrictEqual(found("album:#99"), []);
    // The term for each album finds that album, and no other.
    odd.forEach(function (album) {
        var term = SearchCore.albumTerm(odd, album);
        assert.deepStrictEqual(found(term), album.items, term);
    });
    assert.strictEqual(SearchCore.albumTerm(odd, odd[0]), "album:#3");      // quotes
    assert.strictEqual(SearchCore.albumTerm(odd, odd[1]), "album:3");
    assert.strictEqual(SearchCore.albumTerm(odd, odd[3]), "album:#9");      // as "Cafe"
    assert.strictEqual(SearchCore.albumTerm(odd, odd[4]), "album:\"Summer Trip\"");
    // A start more than one name has: said so, not "no album".
    var result = SearchCore.search(plain, "album:ca", {albums: odd});
    assert.deepStrictEqual(result.items, []);
    assert.ok(/More than one album starts with "ca"/.test(result.warnings.join(" ")),
              result.warnings);
    assert.deepStrictEqual(SearchCore.findAlbum(odd, "ca", true), {album: null,
                                                                   ambiguous: true});
});

test("a full record's marks become compact keys", function () {
    var rec = SITE.compactRecord({id: 7, library_id: "a", name: "x.jpg", path: "x.jpg",
                                  marks: {favorite: true, rating: 4,
                                          albums: [{id: 11, name: "Dogs"},
                                                   {id: 3, name: "A"}]}});
    assert.deepStrictEqual([rec.fav, rec.rt, rec.al], [1, 4, [3, 11]]);
    var none = SITE.compactRecord({id: 8, marks: {favorite: false, rating: 0, albums: []}});
    assert.deepStrictEqual([none.fav, none.rt, none.al], [undefined, undefined, undefined]);
});

test("the search tips name the new words", function () {
    var examples = SearchCore.SYNTAX.map(function (row) {
        return row.example;
    });
    ["is:favorite", "rating:>=4"].forEach(function (example) {
        assert.ok(examples.indexOf(example) >= 0, example);
    });
    assert.ok(examples.some(function (example) {
        return example.indexOf("album:") === 0;
    }));
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
