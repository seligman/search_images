/* Tests for the parts of static/site.js and static/viewer.js that need no
   page: the search state in the address, page addresses, compact records
   made from full ones, and the viewer's formatting and 360-degree sums.

   Run by tests/test_ui_scripts.py, or directly:
       node tests/js/ui_test.js
       node tests/js/ui_test.js --compare FILE   (records made by the server)
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");

var STATIC = path.join(__dirname, "..", "..", "static");
var SearchCore = require(path.join(STATIC, "search-core.js"));
var VIEWER = require(path.join(STATIC, "viewer.js"));

// site.js reads its settings once, when it loads: load it afresh for a
// given page configuration (and protocol of the page's address).
function loadSite(config, protocol) {
    var file = require.resolve(path.join(STATIC, "site.js"));
    delete require.cache[file];
    global.window = config ? {APP_CONFIG: config,
                              location: {search: "", protocol: protocol || "http:"}} : undefined;
    try {
        return require(file);
    } finally {
        delete global.window;
    }
}

var SITE = loadSite({mode: "server", api: "/api", page: "search"});

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

/* ---------- Search state in the address ------------------------------------- */

test("the address keeps a search and reads it back", function () {
    var state = {q: "red car \"on the beach\" -dog", lib: "lib_family1", sort: "oldest",
                 filters: {type: ["photo", "doc:receipt"], tags: ["sun & sea", "a=b"],
                           year: ["2019"]},
                 group: false, size: "l", hidden: true, junk: true, sensitive: "show",
                 ids: "5-2.2", view: "-17.5,179.9,4", from: "2019-03", to: "2019-05-14"};
    var query = SITE.searchState.params(state);
    var back = SITE.searchState.read("?" + query);
    assert.deepStrictEqual(back, state);
    // One parameter per value, named after the facet group.
    assert.ok(/(^|&)type=photo&type=doc%3Areceipt(&|$)/.test(query), query);
    assert.ok(query.indexOf("tags=sun+%26+sea") >= 0, query);
});

test("defaults stay out of the address", function () {
    assert.strictEqual(SITE.searchState.params(SITE.searchState.empty()), "");
    assert.strictEqual(SITE.searchState.params({q: "cat", group: true, filters: {}}), "q=cat");
    var state = SITE.searchState.read("");
    assert.deepStrictEqual(state, SITE.searchState.empty());
    assert.strictEqual(state.group, true);
});

test("values the page does not know are ignored", function () {
    var state = SITE.searchState.read("?sort=random&size=xl&sensitive=all&hidden=yes&junk=0" +
                                      "&group=1&colour=red&type=&type=photo&type=photo");
    assert.strictEqual(state.sort, "");
    assert.strictEqual(state.size, "");
    assert.strictEqual(state.sensitive, "");
    assert.strictEqual(state.hidden, false);
    assert.strictEqual(state.junk, false);
    assert.strictEqual(state.group, true);
    assert.deepStrictEqual(state.filters, {type: ["photo"]});
    var many = [];
    for (var k = 0; k < 80; k++) {
        many.push("tags=t" + k);
    }
    assert.strictEqual(SITE.searchState.read("?" + many.join("&")).filters.tags.length, 50);
    assert.strictEqual(SITE.searchState.read("?q=" + "x".repeat(5000)).q.length, 2000);
});

test("the map's view and the pictures picked on it travel in the address", function () {
    var state = SITE.searchState.read("?q=beach&ids=0-4.a&view=48.8566,2.3522,7");
    assert.strictEqual(state.ids, "0-4.a");
    assert.strictEqual(state.view, "48.8566,2.3522,7");
    // The search page and the pages it links to keep both...
    assert.strictEqual(SITE.searchUrl(state), "/?q=beach&ids=0-4.a&view=48.8566%2C2.3522%2C7");
    assert.strictEqual(SITE.detailUrl(3, state),
                       "/detail?id=3&q=beach&ids=0-4.a&view=48.8566%2C2.3522%2C7");
    // ... the map shows the whole search, at that view.
    assert.strictEqual(SITE.mapUrl(state), "/map?q=beach&view=48.8566%2C2.3522%2C7");
    assert.strictEqual(state.ids, "0-4.a");
    // Anything else is ignored.
    ["view=91,0,3", "view=0,181,3", "view=1,2", "view=a,b,c", "view=1,2,3.5", "view=1,2,123"]
        .forEach(function (bad) {
            assert.strictEqual(SITE.searchState.read("?" + bad).view, "", bad);
        });
    ["ids=1,2", "ids=../x", "ids=A", "ids=1..2", "ids=" + "1.".repeat(120000) + "1"]
        .forEach(function (bad) {
            assert.strictEqual(SITE.searchState.read("?" + bad).ids, "", bad.slice(0, 20));
        });
    assert.strictEqual(SITE.searchState.read("?view=-0.5,-180,0").view, "-0.5,-180,0");
    assert.strictEqual(SITE.searchState.params({ids: "x y", view: "nowhere"}), "");
});

test("lists of numbers in an address are short", function () {
    var numbers = SITE.idList.numbers;
    assert.strictEqual(numbers.encode([10, 5, 7, 6, 6]), "5-2.2");
    assert.deepStrictEqual(numbers.decode("5-2.2", 100), [5, 6, 7, 10]);
    assert.strictEqual(numbers.encode([]), "");
    assert.strictEqual(numbers.encode([0]), "0");
    // Gaps and runs in base 36: thousands of ids in a few characters each.
    var many = [];
    for (var k = 0; k < 3000; k++) {
        many.push(100000 + k * 7 + (k % 3));
    }
    var text = numbers.encode(many);
    assert.ok(text.length < 3 * many.length, text.length);
    assert.deepStrictEqual(numbers.decode(text, Infinity), many.slice().sort(function (a, b) {
        return a - b;
    }));
    var run = [];
    for (var m = 0; m < 5000; m++) {
        run.push(m + 42);
    }
    assert.strictEqual(numbers.encode(run), "16-3uv");
    // Not such a list, or one that names too much: null.
    ["", "-1", "1..2", "1-", "A", "1,2", "zzzzzzzzzzzz"].forEach(function (bad) {
        assert.strictEqual(numbers.decode(bad, 1000000), null, bad);
    });
    assert.strictEqual(numbers.decode("0-zzzzzz", Infinity), null);
    assert.strictEqual(numbers.decode("5", 5), null);
    assert.deepStrictEqual(numbers.decode("4", 5), [4]);
});

test("the server names picked pictures by their ids", function () {
    var text = SITE.idList.encode([12, 3, 4, 5, "40"], []);
    assert.strictEqual(text, "3-2.6.r");
    assert.deepStrictEqual(SITE.idList.decode(text, []), {ids: [3, 4, 5, 12, 40], stale: false});
    assert.strictEqual(SITE.idList.decode("x_1", []), null);
    assert.strictEqual(SITE.idList.encode([], []), "");
    assert.strictEqual(SITE.idList.maxLength(), 30000);
});

test("an export names picked pictures by their place, and notices a new export", function () {
    var site = loadSite({mode: "static", data: "data/", tiles: "tiles/", page: "map",
                         capabilities: {admin: false, static: true}});
    var records = [];
    for (var k = 0; k < 400; k++) {
        records.push({i: "x" + ("00000" + (k * 7919 % 400)).slice(-5) + "abcdef"});
    }
    var picked = records.slice(100, 160).map(function (rec) {
        return rec.i;
    });
    var text = site.idList.encode(picked, records);
    assert.ok(/^[0-9a-z]+_[0-9a-z.-]+$/.test(text), text);
    // Much shorter than the ids themselves.
    assert.ok(text.length < picked.join(".").length / 3, text);
    var back = site.idList.decode(text, records);
    assert.strictEqual(back.stale, false);
    assert.deepStrictEqual(back.ids.slice().sort(), picked.slice().sort());
    // The same pictures in another order (shards loaded otherwise): the same.
    assert.deepStrictEqual(site.idList.decode(text, records.slice().reverse()).ids.sort(),
                           picked.slice().sort());
    // Made again with other pictures: the places name others, so the list is stale.
    var other = records.slice(1).concat([{i: "znewpicture1"}]);
    assert.deepStrictEqual(site.idList.decode(text, other), {ids: [], stale: true});
    assert.strictEqual(site.idList.decode("nonsense", records), null);
    assert.strictEqual(site.idList.encode(["not-there"], records), "");
    // A static host takes short addresses only; from disk anything goes.
    assert.strictEqual(site.idList.maxLength(), 6000);
    var disk = loadSite({mode: "static", data: "data/", capabilities: {static: true}}, "file:");
    assert.strictEqual(disk.idList.maxLength(), 100000);
});

test("the address uses the facet groups of the search engine", function () {
    assert.deepStrictEqual(SITE.searchState.FACET_KEYS, SearchCore.FACETS.map(function (facet) {
        return facet.key;
    }));
});

/* ---------- Page addresses ------------------------------------------------------- */

test("server addresses", function () {
    var state = {q: "beach", filters: {tags: ["sea"]}};
    assert.strictEqual(SITE.searchUrl(state), "/?q=beach&tags=sea");
    assert.strictEqual(SITE.searchUrl(state, 42), "/?q=beach&tags=sea#id=42");
    assert.strictEqual(SITE.searchUrl({}), "/");
    assert.strictEqual(SITE.detailUrl(7, state), "/detail?id=7&q=beach&tags=sea");
    assert.strictEqual(SITE.detailUrl(7), "/detail?id=7");
    assert.strictEqual(SITE.mapUrl(state), "/map?q=beach&tags=sea");
    assert.strictEqual(SITE.thumbUrl({i: 5, n: "x.jpg"}), "/thumb/5");
    assert.strictEqual(SITE.thumbUrl(6), "/thumb/6");
    assert.strictEqual(SITE.previewUrl({i: 5}), "/preview/5");
    assert.strictEqual(SITE.imageUrl({i: 5}), "/image/5");
    assert.strictEqual(SITE.isStatic, false);
});

test("static export addresses are relative", function () {
    var site = loadSite({mode: "static", data: "data/", tiles: "tiles/", page: "search",
                         capabilities: {admin: false, static: true}});
    assert.strictEqual(site.searchUrl({q: "beach"}), "index.html?q=beach");
    assert.strictEqual(site.detailUrl("ab12", {q: "x"}), "detail.html?id=ab12&q=x");
    assert.strictEqual(site.mapUrl({}), "map.html");
    assert.strictEqual(site.thumbUrl({i: "ab12"}), "thumbs/ab12.jpg");
    assert.strictEqual(site.imageUrl({i: "ab12"}), "images/ab12.jpg");
    assert.strictEqual(site.previewUrl({i: "ab12"}), "images/ab12.jpg");
    // Records of older packages carry their own addresses.
    assert.strictEqual(site.thumbUrl({i: 3, _thumb: "data:image/jpeg;base64,AA"}),
                       "data:image/jpeg;base64,AA");
    assert.strictEqual(site.tileUrl(1, 2, 3), "tiles/1/2/3.jpg");
    var noMap = loadSite({mode: "static", data: "data/", capabilities: {static: true, map: false,
                                                                         images: false}});
    assert.strictEqual(noMap.mapUrl({}), null);
    assert.strictEqual(noMap.imageUrl({i: "ab12"}), null);
    assert.strictEqual(noMap.previewUrl({i: "ab12"}), null);
});

test("an export names itself and its pictures' files", function () {
    assert.strictEqual(SITE.title, "Image Search");
    assert.strictEqual(SITE.originalLabel, "Open the original");
    var web = loadSite({mode: "static", data: "data/", title: "Our Trips",
                        capabilities: {static: true, images: true, originals: false}});
    assert.strictEqual(web.title, "Our Trips");
    assert.strictEqual(web.originalLabel, "Open the large picture");
    // An original kept in its own format says so (rec.ix); nothing else
    // can become part of the address.
    assert.strictEqual(web.imageUrl({i: "ab12", ix: "png"}), "images/ab12.png");
    assert.strictEqual(web.imageUrl({i: "ab12", ix: "../x"}), "images/ab12.jpg");
    assert.strictEqual(web.imageUrl({i: "ab12", ix: 5}), "images/ab12.jpg");
    var originals = loadSite({mode: "static", data: "data/",
                              capabilities: {static: true, images: true, originals: true}});
    assert.strictEqual(originals.originalLabel, "Open the original");
    assert.strictEqual(originals.title, "Image Search");
});

/* ---------- Compact records from full ones ------------------------------------ */

var DETAIL = {
    id: 12, library: "Family", library_id: "lib_family1", path: "2019/Beach/IMG_1.jpg",
    name: "IMG_1.jpg", width: 4000, height: 3000, projection: "equirectangular",
    taken_at: "2019-05-14T10:12:00", camera: "Apple iPhone 12", lat: 1.5, lon: 2.5,
    hidden: true, provenance: {model: "m", prompt_ver: 5},
    info: {title: " Dog on a beach ", description: "A dog.", tags: ["Dog", "dog", " Beach ", ""],
           colors: ["Blue"], subjects: ["dog"], text: "", photo_type: "photo",
           document_type: "none", setting: "unknown", time_of_day: "day", event: "none",
           people: "none", sensitive: "none", private_info: true, aesthetic: 4, sharpness: 9,
           objects: [{name: "ball", count: 2}], mood: "neutral", activities: ["running"],
           landmarks: ["pier"], location_guess: "seaside"}
};

test("a full record becomes the compact search record", function () {
    var rec = SITE.compactRecord(DETAIL);
    assert.deepStrictEqual(rec, {
        i: 12, l: "lib_family1", n: "IMG_1.jpg", f: "2019/Beach", t: "Dog on a beach",
        d: "A dog.", s: ["dog"], g: ["dog", "beach"], c: ["blue"], td: "day", ty: "photo",
        pp: "none", ex: "ball, pier, running, seaside", q: 4, pr: 1, ds: 1,
        dk: "2019-05-14", ts: "2019-05-14T10:12:00", cm: "Apple iPhone 12", gp: 1, w: 4000,
        hgt: 3000, p: "360", h: 1
    });
    var old = SITE.compactRecord({id: 3, library: "Old", path: "a.jpg", name: "a.jpg",
                                  info: {description: "An old one", tags: ["X"],
                                         sharpness: 0.8}});
    assert.strictEqual(old.v4, 1);
    // Version 4 rated sharpness from 0 to 1: no "1/5" for a sharp picture.
    assert.ok(!("sh" in old));
    assert.strictEqual(old.l, "Old");
    assert.ok(!("f" in old));
    var bare = SITE.compactRecord({id: 4, library_id: "lib_x", name: "b.jpg", path: "b.jpg",
                                   info: null});
    assert.deepStrictEqual(bare, {i: 4, l: "lib_x", n: "b.jpg"});
    assert.strictEqual(SITE.compactRecord(null), null);
    // The place: the town only when one is near, and how far it is.
    var placed = SITE.compactRecord({id: 5, library_id: "lib_x", name: "c.jpg", path: "c.jpg",
                                     lat: 20.9, lon: -156.5,
                                     place: {city: "Kahului", region: "Hawaii",
                                             country: "United States", cc: "US", km: 2.6}});
    assert.deepStrictEqual(placed, {i: 5, l: "lib_x", n: "c.jpg", gp: 1, ci: "Kahului",
                                    rg: "Hawaii", co: "United States", cc: "US", km: 3});
    var far = SITE.compactRecord({id: 6, library_id: "lib_x", name: "d.jpg", path: "d.jpg",
                                  place: {city: "", region: "", country: "Fiji", cc: "FJ"}});
    assert.deepStrictEqual(far, {i: 6, l: "lib_x", n: "d.jpg", co: "Fiji", cc: "FJ"});
});

test("the timeline's dates and the shuffle seed travel in the address", function () {
    var state = SITE.searchState.read("?from=2019-03&to=2019-05-31&sort=shuffle");
    assert.strictEqual(state.from, "2019-03");
    assert.strictEqual(state.to, "2019-05-31");
    assert.strictEqual(state.sort, "shuffle");
    ["2019-13", "2019-02-32", "19", "2019-1", "x", "2019-05-14T10:00"].forEach(function (value) {
        var read = SITE.searchState.read("?from=" + encodeURIComponent(value) + "&to=" +
                                         encodeURIComponent(value));
        assert.strictEqual(read.from, "", value);
        assert.strictEqual(read.to, "", value);
        assert.strictEqual(SITE.searchState.params({from: value, to: value}), "", value);
    });
    // One seed per page (sessionStorage when there is one), a new one when asked.
    var seed = SITE.shuffleSeed();
    assert.strictEqual(SITE.shuffleSeed(), seed);
    assert.ok(seed >= 0 && seed < 4294967296 && seed % 1 === 0);
    var renewed = SITE.shuffleSeed(true);
    assert.strictEqual(SITE.shuffleSeed(), renewed);
});

/* ---------- Viewer: words and numbers ------------------------------------------ */

var format = VIEWER.format;

test("dates in words", function () {
    assert.strictEqual(format.monthLabel("2019-05-14"), "May 2019");
    assert.strictEqual(format.monthLabel("2019-05-01T00:00:00"), "May 2019");
    assert.strictEqual(format.shortDate("2019-12-03"), "Dec 3, 2019");
    assert.strictEqual(format.longDate("2019-05-14T10:02:00"), "Tuesday, May 14, 2019 at 10:02");
    assert.strictEqual(format.longDate("2000-02-29"), "Tuesday, February 29, 2000");
    ["", null, undefined, "2019", "2019-13-01", "junk", "2019-00-10"].forEach(function (value) {
        assert.strictEqual(format.monthLabel(value), "");
        assert.strictEqual(format.longDate(value), "");
    });
});

test("places in words", function () {
    assert.strictEqual(format.placeText({co: "United States", rg: "Hawaii", ci: "Kahului",
                                         km: 3}), "Kahului, Hawaii, United States (3 km)");
    assert.strictEqual(format.placeText({co: "France", rg: "Bretagne", ci: "Brest", km: 0}),
                       "Brest, Bretagne, France (under 1 km)");
    assert.strictEqual(format.placeText({co: "Singapore", ci: "Singapore"}),
                       "Singapore, Singapore");
    assert.strictEqual(format.placeText({co: "Fiji", rg: "Northern"}), "Northern, Fiji");
    assert.strictEqual(format.placeText({ci: "Nowhere"}), "");
    assert.strictEqual(format.placeText(null), "");
});

test("camera settings in words", function () {
    assert.deepStrictEqual(format.exposureParts({ExposureTime: 0.008, FNumber: 2.8,
                                                 ISOSpeedRatings: 100, FocalLength: 4.25,
                                                 FocalLengthIn35mmFilm: 26}),
                           ["1/125 s", "f/2.8", "ISO 100", "4.3 mm (26 mm full frame)"]);
    assert.deepStrictEqual(format.exposureParts({ExposureTime: 2.5, FNumber: 8,
                                                 PhotographicSensitivity: 3200,
                                                 FocalLength: 50, FocalLengthIn35mmFilm: 50}),
                           ["2.5 s", "f/8", "ISO 3200", "50 mm"]);
    assert.deepStrictEqual(format.exposureParts({ISOSpeedRatings: [400, 400]}), ["ISO 400"]);
    assert.deepStrictEqual(format.exposureParts({ExposureTime: null, FNumber: 0,
                                                 FocalLength: "x"}), []);
    assert.deepStrictEqual(format.exposureParts(null), []);
});

test("file sizes", function () {
    assert.strictEqual(format.fileSize(512), "512 bytes");
    assert.strictEqual(format.fileSize(2048), "2.0 KB");
    assert.strictEqual(format.fileSize(5 * 1024 * 1024 + 1), "5.0 MB");
    assert.strictEqual(format.fileSize(300 * 1024 * 1024), "300 MB");
    assert.strictEqual(format.fileSize(null), "");
});

test("highlight ranges become text pieces", function () {
    assert.deepStrictEqual(format.segments("a red car", [[2, 5]]),
                           [{text: "a ", mark: false}, {text: "red", mark: true},
                            {text: " car", mark: false}]);
    assert.deepStrictEqual(format.segments("abc", []), [{text: "abc", mark: false}]);
    assert.deepStrictEqual(format.segments("", []), [{text: "", mark: false}]);
    // Overlapping, out of range and broken ranges are cut to what is left.
    assert.deepStrictEqual(format.segments("abcdef", [[0, 2], [1, 3], [5, 99], ["x", 2], null]),
                           [{text: "ab", mark: true}, {text: "c", mark: true},
                            {text: "de", mark: false}, {text: "f", mark: true}]);
    // With the engine's own ranges, every character is kept exactly once.
    var text = "Gere-a-Deli\nDELI SPECIALS, the deli's best";
    var pieces = format.segments(text, SearchCore.highlights(text, "deli"));
    assert.strictEqual(pieces.map(function (piece) {
        return piece.text;
    }).join(""), text);
    assert.deepStrictEqual(pieces.filter(function (piece) {
        return piece.mark;
    }).map(function (piece) {
        return piece.text;
    }), ["Deli", "DELI", "deli's"]);
});

/* ---------- Viewer: the 360-degree view ------------------------------------------ */

function near(actual, expected, message) {
    assert.ok(Math.abs(actual - expected) < 1e-6, message + ": " + actual + " != " + expected);
}

test("the 360 view looks at the middle of the panorama at first", function () {
    var tangents = format.panoTangents(75 * Math.PI / 180, 1600, 900);
    var centre = format.panoLookup(0, 0, 0, 0, tangents);
    near(centre.u, 0.5, "u");
    near(centre.v, 0.5, "v");
    // The right edge of the view is further right on the panorama.
    assert.ok(format.panoLookup(0, 0, 1, 0, tangents).u > 0.5);
    // Up is towards the top row (v = 0); straight up is the top row.
    assert.ok(format.panoLookup(0, 0, 0, 1, tangents).v < 0.5);
    near(format.panoLookup(0, Math.PI / 2, 0, 0, tangents).v, 0, "v looking straight up");
    // Turning by half a circle looks at the seam (u = 0 or 1).
    var behind = format.panoLookup(Math.PI, 0, 0, 0, tangents).u;
    assert.ok(behind < 1e-6 || behind > 1 - 1e-6, "behind: " + behind);
    // The longer side gets the field of view.
    var tall = format.panoTangents(Math.PI / 2, 900, 1600);
    near(tall.y, 1, "portrait");
    near(format.panoTangents(Math.PI / 2, 1600, 900).x, 1, "landscape");
});

test("dragging turns the 360 view with the finger, within limits", function () {
    var view = {yaw: 0, pitch: 0, fov: Math.PI / 2};
    var right = format.panoDrag(view, 100, 0, 1000, 500);
    assert.ok(right.yaw > 0);
    // Content follows the finger: what was left of the middle comes to the middle.
    var looked = format.panoLookup(right.yaw, 0, 0, 0, format.panoTangents(view.fov, 1000, 500));
    assert.ok(looked.u < 0.5, "u " + looked.u);
    var down = format.panoDrag(view, 0, 100000, 1000, 500);
    near(down.pitch, Math.PI / 2 - 0.01, "pitch limit");
    var spun = format.panoDrag(view, 1e6, 0, 1000, 500);
    assert.ok(spun.yaw >= -Math.PI && spun.yaw < Math.PI, "yaw wraps: " + spun.yaw);
    near(format.panoZoom(view, 0.01).fov, format.MIN_FOV, "zoom in limit");
    near(format.panoZoom(view, 100).fov, format.MAX_FOV, "zoom out limit");
});

test("the edit form's tags and what a save sends", function () {
    assert.deepStrictEqual(format.splitTags(" Rex, grandma's  garden;REX,, "),
                           ["rex", "grandma's garden"]);
    assert.deepStrictEqual(format.splitTags(""), []);
    assert.strictEqual(format.tagKey("  Golden   Retriever "), "golden retriever");
    // The owner's tags first, then the model's that were not removed.
    assert.deepStrictEqual(format.editedTags(["Dog", "beach", "sand"], ["rex", "beach"],
                                             ["SAND"]),
                           [{tag: "rex", added: true}, {tag: "beach", added: true},
                            {tag: "dog", added: false}]);
    assert.deepStrictEqual(format.editedTags(null, null, null), []);
    assert.deepStrictEqual(format.editedTags(["constructor"], ["__proto__"], []),
                           [{tag: "__proto__", added: true}, {tag: "constructor", added: false}]);
    var model = {title: "Dog on a beach", description: "A dog runs."};
    // Text like the model's (or none) is sent as null: the model's is used.
    assert.deepStrictEqual(format.editBody(model, {title: " Dog on a beach ", description: "",
                                                   added: ["rex"], removed: ["sand"]}),
                           {title: null, description: null, tags_add: ["rex"],
                            tags_remove: ["sand"]});
    assert.deepStrictEqual(format.editBody(null, {title: "Rex", description: "Our dog.  "}),
                           {title: "Rex", description: "Our dog.", tags_add: [],
                            tags_remove: []});
    // A tag longer than the server keeps is named before anything is sent.
    assert.strictEqual(format.tagProblem(["rex", "x".repeat(60)]), "");
    assert.strictEqual(format.tagProblem([]), "");
    var problem = format.tagProblem(["rex", "y".repeat(61)]);
    assert.ok(problem.indexOf("y".repeat(24) + "\u2026") > 0, problem);
    assert.ok(/at most 60 characters/.test(problem), problem);
});

test("the viewer loads without a page and only offers its helpers", function () {
    assert.deepStrictEqual(Object.keys(VIEWER), ["format"]);
});

test("the 360 view gets the large panorama of 360 photos only", function () {
    assert.strictEqual(SITE.panoUrl({i: 1, p: "360", w: 6080, hgt: 3040}), "/pano/1");
    assert.strictEqual(SITE.panoUrl({i: 2, w: 8192, hgt: 4096}), null);
    assert.strictEqual(SITE.panoUrl(null), null);
    assert.strictEqual(SITE.panoUrl(3), null);
    // A 360 photo no larger than its preview has none: the preview is all of it.
    assert.strictEqual(SITE.panoUrl({i: 4, p: "360", w: 2048, hgt: 1024}), null);
    assert.strictEqual(SITE.panoUrl({i: 5, p: "360", w: 2050, hgt: 1025}), "/pano/5");
    // An export ships panoramas only when it says so, for the pictures
    // that have one (pn).
    var bare = loadSite({mode: "static", data: "data/", capabilities: {static: true}});
    assert.strictEqual(bare.panoUrl({i: "ab12", p: "360", pn: 1}), null);
    var withPanos = loadSite({mode: "static", data: "data/",
                              capabilities: {static: true, panos: true}});
    assert.strictEqual(withPanos.panoUrl({i: "ab12", p: "360", pn: 1}), "panos/ab12.jpg");
    assert.strictEqual(withPanos.panoUrl({i: "ab15", p: "360"}), null);
    assert.strictEqual(withPanos.panoUrl({i: "ab13"}), null);
    assert.strictEqual(withPanos.panoUrl({i: "ab14", p: "360", _pano: "x/p.jpg"}), "x/p.jpg");
    // The viewer no longer decides on its own which larger picture to load.
    assert.strictEqual(VIEWER.format.wantsSharp, undefined);
});

/* ---------- Loading the index (fetch is faked) -------------------------------- */

// A fake fetch answering with the given ETags in turn; reads counts the
// answers whose body was parsed.
function fakeFetch(tags, counter) {
    var calls = 0;
    return function () {
        var tag = tags[Math.min(calls, tags.length - 1)];
        calls += 1;
        var number = calls;
        return Promise.resolve({
            ok: true,
            headers: {get: function (name) {
                return name.toLowerCase() === "etag" ? tag : null;
            }},
            json: function () {
                counter.reads += 1;
                return Promise.resolve({version: 1, libraries: [], records: [{i: number}]});
            },
            body: {cancel: function () {
                counter.cancelled += 1;
                return Promise.resolve();
            }}
        });
    };
}

function withFetch(fake, body) {
    var saved = global.fetch;
    global.fetch = fake;
    return body().then(function (value) {
        global.fetch = saved;
        return value;
    }, function (error) {
        global.fetch = saved;
        throw error;
    });
}

function call(method) {
    return new Promise(function (resolve) {
        method(resolve);
    });
}

test("an unchanged index is not parsed again when the page refreshes it", function () {
    var site = loadSite({mode: "server", api: "/api", page: "search"});
    var counter = {reads: 0, cancelled: 0};
    // The first answer, then the same records (gzip ETag), then new ones.
    var fake = fakeFetch(['"abc"', '"abc-gzip"', 'W/"abc"', '"def-gzip"'], counter);
    return withFetch(fake, function () {
        return call(site.loadIndex).then(function (data) {
            assert.strictEqual(data.records[0].i, 1);
            assert.strictEqual(counter.reads, 1);
            return call(site.refreshIndex);
        }).then(function (data) {
            assert.strictEqual(data, null);
            assert.strictEqual(counter.reads, 1, "the unchanged body is not parsed");
            assert.strictEqual(counter.cancelled, 1);
            return call(site.refreshIndex);
        }).then(function (data) {
            assert.strictEqual(data, null);
            assert.strictEqual(counter.reads, 1);
            return call(site.refreshIndex);
        }).then(function (data) {
            assert.strictEqual(data.records[0].i, 4);
            assert.strictEqual(counter.reads, 2);
            return call(site.refreshIndex);
        }).then(function (data) {
            assert.strictEqual(data, null, "the new records are known now");
        });
    });
});

test("the page hears about the index once, even when its own code fails", function () {
    var site = loadSite({mode: "server", api: "/api", page: "search"});
    var counter = {reads: 0, cancelled: 0};
    var calls = [];
    var thrown = [];
    function caught(error) {
        thrown.push(String(error && error.message));
    }
    process.on("uncaughtException", caught);
    return withFetch(fakeFetch(['"abc"'], counter), function () {
        site.loadIndex(function (data) {
            calls.push(data ? "data" : "null");
            throw new Error("a bug in the page");
        });
        return new Promise(function (resolve) {
            setTimeout(resolve, 50);
        });
    }).then(function () {
        process.removeListener("uncaughtException", caught);
        assert.deepStrictEqual(calls, ["data"]);
        assert.deepStrictEqual(thrown, ["a bug in the page"]);
    }, function (error) {
        process.removeListener("uncaughtException", caught);
        throw error;
    });
});

test("a failed download gives null", function () {
    var site = loadSite({mode: "server", api: "/api", page: "search"});
    var fake = function () {
        return Promise.resolve({ok: false, status: 500, headers: {get: function () {
            return null;
        }}});
    };
    return withFetch(fake, function () {
        return call(site.loadIndex).then(function (data) {
            assert.strictEqual(data, null);
            return call(site.refreshIndex);
        }).then(function (data) {
            assert.strictEqual(data, null);
        });
    });
});

/* ---------- The picture stage on a fake page ---------------------------------- */

// Just enough of a page for VIEWER.Stage: elements, images that load (or
// fail) when the test says so, a WebGL context that takes any picture
// (options.webgl) and a fetch the test answers. log records what the
// stage asked for.
function fakePage(options) {
    var log = {images: [], fetches: [], uploads: [], aborted: [], closed: 0};

    function Element(tag) {
        var classes = {};
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.attributes = {};
        this.style = {};
        this.hidden = false;
        this.clientWidth = 800;
        this.clientHeight = 600;
        this.classList = {
            add: function (name) {
                classes[name] = true;
            },
            remove: function (name) {
                delete classes[name];
            },
            toggle: function (name, on) {
                if (on === undefined ? !classes[name] : on) {
                    classes[name] = true;
                } else {
                    delete classes[name];
                }
            },
            contains: function (name) {
                return Boolean(classes[name]);
            }
        };
    }
    Element.prototype.setAttribute = function (name, value) {
        this.attributes[name] = String(value);
        if (name === "src") {
            this.src = String(value);
        }
    };
    Element.prototype.getAttribute = function (name) {
        return name in this.attributes ? this.attributes[name] : null;
    };
    Element.prototype.removeAttribute = function (name) {
        delete this.attributes[name];
        if (name === "src") {
            this.src = "";
        }
    };
    Element.prototype.appendChild = function (child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    };
    Element.prototype.insertBefore = function (child, before) {
        var at = this.children.indexOf(before);
        child.parentNode = this;
        this.children.splice(at < 0 ? this.children.length : at, 0, child);
        return child;
    };
    Element.prototype.removeChild = function (child) {
        this.children.splice(this.children.indexOf(child), 1);
        child.parentNode = null;
        return child;
    };
    Element.prototype.addEventListener = function () {};
    Element.prototype.removeEventListener = function () {};
    Element.prototype.getContext = function () {
        return options.webgl ? fakeGl(log, options.maxTexture || 8192) : null;
    };

    // new Image(): loads when the test calls finish(width, height), fails
    // with finish().
    function FakeImage() {
        Element.call(this, "img");
        this.naturalWidth = 0;
        this.naturalHeight = 0;
        log.images.push(this);
    }
    FakeImage.prototype = Object.create(Element.prototype);
    FakeImage.prototype.finish = function (width, height) {
        if (width) {
            this.naturalWidth = width;
            this.naturalHeight = height;
            this.onload();
        } else {
            this.onerror();
        }
    };

    var window = {
        location: {protocol: "http:"},
        devicePixelRatio: 1,
        addEventListener: function () {},
        removeEventListener: function () {},
        requestAnimationFrame: function () {
            return 1;
        },
        cancelAnimationFrame: function () {}
    };
    if (options.fetch) {
        // Answered by the test: call.answer(width, height) or call.fail().
        window.fetch = function (url, init) {
            var call = {url: url};
            log.fetches.push(call);
            return new Promise(function (resolve, reject) {
                call.answer = function (width, height) {
                    resolve({ok: true, blob: function () {
                        return Promise.resolve({width: width, height: height});
                    }});
                };
                call.fail = function () {
                    resolve({ok: false, status: 404});
                };
                init.signal.addEventListener("abort", function () {
                    log.aborted.push(url);
                    reject(new Error("aborted"));
                });
            });
        };
        window.createImageBitmap = function (blob) {
            return Promise.resolve({width: blob.width, height: blob.height, close: function () {
                log.closed += 1;
            }});
        };
    }
    var document = {
        createElement: function (tag) {
            return new Element(tag);
        },
        createTextNode: function (text) {
            return {textContent: text};
        }
    };
    return {log: log, window: window, document: document, Image: FakeImage,
            box: new Element("div")};
}

// A WebGL context that compiles anything, takes textures up to maxTexture
// wide and records the pictures uploaded.
function fakeGl(log, maxTexture) {
    var gl = {
        getShaderParameter: function () {
            return true;
        },
        getProgramParameter: function () {
            return true;
        },
        getParameter: function () {
            return maxTexture;
        },
        getError: function () {
            return 0;
        },
        NO_ERROR: 0,
        isContextLost: function () {
            return false;
        },
        getExtension: function () {
            return null;
        },
        texImage2D: function () {
            log.uploads.push(arguments[arguments.length - 1]);
        }
    };
    return new Proxy(gl, {get: function (target, name) {
        if (name in target) {
            return target[name];
        }
        return /^[A-Z0-9_]+$/.test(String(name)) ? String(name) : function () {
            return {};
        };
    }});
}

// Runs body(VIEWER, page) with the fake page installed as the globals the
// viewer uses; body may return a promise.
function onFakePage(options, body) {
    var page = fakePage(options);
    var saved = {window: global.window, document: global.document, Image: global.Image};
    global.window = page.window;
    global.document = page.document;
    global.Image = page.Image;
    function restore() {
        Object.keys(saved).forEach(function (name) {
            if (saved[name] === undefined) {
                delete global[name];
            } else {
                global[name] = saved[name];
            }
        });
    }
    var file = require.resolve(path.join(STATIC, "viewer.js"));
    delete require.cache[file];
    var viewer = require(file);
    delete require.cache[file];
    return Promise.resolve().then(function () {
        return body(viewer, page);
    }).then(function (value) {
        restore();
        return value;
    }, function (error) {
        restore();
        throw error;
    });
}

// Lets the promises in flight settle.
function settle() {
    return new Promise(function (resolve) {
        setImmediate(resolve);
    });
}

var PANO_REC = {i: 14, p: "360", w: 6080, hgt: 3040, t: "Ferns"};
var PANO_URLS = {thumb: "/thumb/14", preview: "/preview/14", pano: "/pano/14"};

test("the large panorama is loaded only once the 360 view runs on the preview", function () {
    return onFakePage({webgl: true, fetch: true}, function (viewer, page) {
        var stage = new viewer.Stage(page.box, {});
        stage.show(PANO_REC, PANO_URLS);
        // Only the preview at first: the panorama would slow it down.
        assert.deepStrictEqual(page.log.images.map(function (image) {
            return image.src;
        }), ["/preview/14"]);
        assert.strictEqual(page.log.fetches.length, 0);
        page.log.images[0].finish(2048, 1024);
        assert.strictEqual(stage.mode, "pano");
        assert.strictEqual(page.box.getAttribute("data-texture-width"), "2048");
        assert.deepStrictEqual(page.log.fetches.map(function (call) {
            return call.url;
        }), ["/pano/14"]);
        page.log.fetches[0].answer(4096, 2048);
        return settle().then(function () {
            // Decoded off the page's thread (an ImageBitmap), uploaded, freed.
            assert.strictEqual(page.log.uploads.length, 2);
            assert.strictEqual(page.log.uploads[1].width, 4096);
            assert.strictEqual(page.box.getAttribute("data-texture-width"), "4096");
            assert.strictEqual(page.log.closed, 1);
            stage.destroy();
        });
    });
});

test("a browser without the 360 view never asks for the large panorama", function () {
    return onFakePage({webgl: false, fetch: true}, function (viewer, page) {
        var stage = new viewer.Stage(page.box, {});
        stage.show(PANO_REC, PANO_URLS);
        page.log.images[0].finish(2048, 1024);
        assert.strictEqual(stage.mode, "image");
        assert.strictEqual(stage.img.src, "/preview/14");
        assert.ok(/flat panorama/.test(stage.note.textContent));
        return settle().then(function () {
            assert.strictEqual(page.log.fetches.length, 0);
            assert.strictEqual(page.log.images.length, 1);
            stage.destroy();
        });
    });
});

test("nor does one that can show nothing wider than the preview", function () {
    return onFakePage({webgl: true, fetch: true, maxTexture: 2048}, function (viewer, page) {
        var stage = new viewer.Stage(page.box, {});
        stage.show(PANO_REC, PANO_URLS);
        page.log.images[0].finish(2048, 1024);
        assert.strictEqual(stage.mode, "pano");
        return settle().then(function () {
            assert.strictEqual(page.log.fetches.length, 0);
            stage.destroy();
        });
    });
});

test("without a preview the large panorama starts the 360 view", function () {
    return onFakePage({webgl: true, fetch: true}, function (viewer, page) {
        var stage = new viewer.Stage(page.box, {});
        stage.show(PANO_REC, PANO_URLS);
        page.log.images[0].finish();                    // the preview fails
        assert.strictEqual(page.log.fetches.length, 1);
        page.log.fetches[0].answer(4096, 2048);
        return settle().then(function () {
            assert.strictEqual(stage.mode, "pano");
            assert.strictEqual(page.box.getAttribute("data-texture-width"), "4096");
            assert.strictEqual(stage.note.hidden, true);
            // Neither: say so.
            stage.show(PANO_REC, PANO_URLS);
            page.log.images[1].finish();
            page.log.fetches[1].fail();
            return settle();
        }).then(function () {
            assert.strictEqual(stage.mode, "image");
            assert.ok(/cannot be read/.test(stage.note.textContent));
            stage.destroy();
        });
    });
});

test("moving on stops loading the large panorama", function () {
    return onFakePage({webgl: true, fetch: true}, function (viewer, page) {
        var stage = new viewer.Stage(page.box, {});
        stage.show(PANO_REC, PANO_URLS);
        page.log.images[0].finish(2048, 1024);
        stage.show({i: 15, w: 4000, hgt: 3000}, {thumb: "/thumb/15", preview: "/preview/15"});
        assert.deepStrictEqual(page.log.aborted, ["/pano/14"]);
        page.log.fetches[0].answer(4096, 2048);
        return settle().then(function () {
            assert.strictEqual(page.log.uploads.length, 1);
            assert.strictEqual(stage.mode, "image");
            stage.destroy();
        });
    });
});

test("without fetch the large panorama loads as an image", function () {
    return onFakePage({webgl: true, fetch: false}, function (viewer, page) {
        var stage = new viewer.Stage(page.box, {});
        stage.show(PANO_REC, PANO_URLS);
        page.log.images[0].finish(2048, 1024);
        assert.strictEqual(page.log.images.length, 2);
        assert.strictEqual(page.log.images[1].src, "/pano/14");
        page.log.images[1].finish(4096, 2048);
        assert.strictEqual(page.box.getAttribute("data-texture-width"), "4096");
        stage.destroy();
    });
});

/* ---------- Records made by the server (tests/test_ui_scripts.py) ------------- */

function compareFile(file) {
    var data = JSON.parse(fs.readFileSync(file, "utf-8"));
    data.pairs.forEach(function (pair, index) {
        test("server record " + index + " (id " + pair.record.i + ") matches its full record",
             function () {
                 var expected = Object.assign({}, pair.record);
                 // Duplicate groups are worked out from all the records.
                 delete expected.dg;
                 assert.deepStrictEqual(SITE.compactRecord(pair.detail), expected);
             });
    });
}

var at = process.argv.indexOf("--compare");
if (at >= 0) {
    tests = [];
    compareFile(process.argv[at + 1]);
}

/* ---------- Run ------------------------------------------------------------ */

var failures = 0;

function failed(entry, error) {
    failures += 1;
    console.log("FAIL - " + entry.name);
    console.log(String(error && error.stack || error).split("\n").map(function (line) {
        return "    " + line;
    }).join("\n"));
}

// One test after another; a test may return a promise.
function runFrom(index) {
    if (index >= tests.length) {
        finish();
        return;
    }
    var entry = tests[index];
    var outcome;
    try {
        outcome = Promise.resolve(entry.body());
    } catch (error) {
        failed(entry, error);
        runFrom(index + 1);
        return;
    }
    outcome.then(function () {
        console.log("ok - " + entry.name);
    }, function (error) {
        failed(entry, error);
    }).then(function () {
        runFrom(index + 1);
    });
}

function finish() {
    console.log((tests.length - failures) + " of " + tests.length + " passed");
    process.exitCode = failures ? 1 : 0;
}

runFrom(0);
