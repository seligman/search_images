/* Tests for the search by drawing on the map: the text of an area
   (SearchCore.areas: read, encode, has, label, bounds, simplify), the area
   option of SearchCore.search (the pictures' own positions, estimated
   places deep enough inside, a drive's clip by its route, smart albums with
   an area), the area in the address (SITE.searchState), the search page
   (the "Area: ..." chip, the places loaded when needed, Save as album, a
   site without locations) and the map page (an area from the address, its
   chip, This view, a rectangle and a shape drawn with the mouse -- a lasso
   that crosses itself --, Esc, the keyboard's focus, Street or place with a
   made-up STREETS.Picker: its list before and after the street maps are read,
   the Which one? menu, a touch screen, the Within list) -- the real page
   scripts on the made-up DOM of tests/js/app_meaning_test.js, with a fake
   fetch() for the server's answers. Every place here is made up.

   Run by tests/test_map_areas.py, or directly: node tests/js/map_areas_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var ROOT = path.join(__dirname, "..", "..");
var STATIC = path.join(ROOT, "static");
var SearchCore = require(path.join(STATIC, "search-core.js"));
var SITE = require(path.join(STATIC, "site.js"));
var FAKE = require(path.join(__dirname, "app_meaning_test.js"));
var A = SearchCore.areas;

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

// (Made-up places around 45 N 10 E, and one far away.)
var BOX = A.encode("b", [[44.99, 9.98, 45.01, 10.02]]);
var ROAD = A.encode("l", [[45.0, 9.99, 45.0, 10.01]], 50);
var SPOT = A.encode("l", [[45.0, 10.0]], 100);
var SHAPE = A.encode("p", [[44.99, 9.99, 45.01, 9.99, 45.0, 10.02]]);

/* ---------- The text of an area ----------------------------------------------- */

test("an area's text reads back: kind, metres, parts; only letters, digits and - _ .",
     function () {
         [BOX, ROAD, SPOT, SHAPE].forEach(function (text) {
             assert.ok(/^[bpl][0-9]*(\.[0-9A-Za-z_-]+)+$/.test(text), text);
             assert.strictEqual(A.read(text).text, text);
             // (An area read already is given back as it is.)
             assert.strictEqual(A.read(A.read(text)), A.read(text));
         });
         var road = A.read(ROAD);
         assert.strictEqual(road.kind, "l");
         assert.strictEqual(road.metres, 50);
         assert.deepStrictEqual(road.parts, [[45, 9.99, 45, 10.01]]);
         // A rectangle is read as its four corners.
         assert.deepStrictEqual(A.read(BOX).parts, [[44.99, 9.98, 45.01, 9.98, 45.01, 10.02,
                                                     44.99, 10.02]]);
         // Several parts; a longitude past 180 stays with the points before it.
         var two = A.encode("p", [[1, 179.99, 1.01, 179.99, 1.01, 180.02],
                                  [-1, 20, -1.01, 20, -1.01, 20.01]]);
         assert.deepStrictEqual(A.read(two).parts[0], [1, 179.99, 1.01, 179.99, 1.01, 180.02]);
         assert.strictEqual(A.read(two).parts.length, 2);
     });

test("text that is no area is not read, and nothing makes one", function () {
    ["", "b", "x.AA", "b.AAAA.AAAA", "p.AAAA", "l.AAAA", "l0.AAAA", "l6000.AAAA",
     "b20.AAAA", "p.A A", "l50.AAAA!", "l50.A_", "l050.AAAA"].forEach(function (bad) {
        assert.strictEqual(A.read(bad), null, bad);
    });
    // A rectangle needs its north-east corner north and east of the other.
    assert.strictEqual(A.encode("b", [[45, 10, 44, 11]]), "");
    assert.strictEqual(A.encode("b", [[45, 10, 46, 9]]), "");
    // A polygon needs three points, a line one; the kind must be known.
    assert.strictEqual(A.encode("p", [[45, 10, 46, 11]]), "");
    assert.strictEqual(A.encode("l", [[]], 50), "");
    assert.strictEqual(A.encode("q", [[45, 10]], 50), "");
    // Latitudes beyond the poles, and too many points or parts.
    assert.strictEqual(A.encode("l", [[95, 10]], 50), "");
    var many = [];
    for (var k = 0; k < A.MAX_POINTS + 1; k++) {
        many.push(45 + k * 1e-4, 10);
    }
    assert.strictEqual(A.encode("l", [many], 50), "");
    var parts = [];
    for (var p = 0; p < 201; p++) {
        parts.push([45, 10 + p * 0.01]);
    }
    assert.strictEqual(A.encode("l", parts, 50), "");
    // Not more than AREA_MAX_TEXT characters.
    assert.strictEqual(A.read("l50." + new Array(A.MAX_TEXT).join("A")), null);
});

test("what is in an area: rectangle, shape, a street's width, a point's circle", function () {
    assert.ok(A.has(BOX, 45, 10));
    assert.ok(!A.has(BOX, 45.02, 10));
    assert.ok(!A.has(BOX, 45, 10.03));
    assert.ok(A.has(SHAPE, 45, 10));
    assert.ok(!A.has(SHAPE, 45.009, 10.015));
    // 50 m either side of the road: 40 m off is in, 60 m off is not (a degree of
    // latitude is about 111 km).
    assert.ok(A.has(ROAD, 45 + 40 / 111195, 10));
    assert.ok(!A.has(ROAD, 45 + 60 / 111195, 10));
    // Past the road's ends: only around them.
    assert.ok(A.has(ROAD, 45, 10.01 + 30 / 78700));
    assert.ok(!A.has(ROAD, 45, 10.01 + 80 / 78700));
    assert.ok(A.has(SPOT, 45 + 90 / 111195, 10));
    assert.ok(!A.has(SPOT, 45 + 110 / 111195, 10));
    // Deep enough inside: within 100 m of the point, 30 m deep is 70 m from it.
    assert.ok(A.has(SPOT, 45 + 60 / 111195, 10, 30));
    assert.ok(!A.has(SPOT, 45 + 80 / 111195, 10, 30));
    // The rectangle's nearest edge is about 1.1 km from its middle.
    assert.ok(A.has(BOX, 45, 10, 1000));
    assert.ok(!A.has(BOX, 45, 10, 1200));
    // Not a place: no.
    assert.ok(!A.has(BOX, NaN, 10));
    assert.ok(!A.has("not an area", 45, 10));
});

test("an area across the date line holds the places on both sides of it", function () {
    var across = A.encode("b", [[-17, 179.9, -16.9, 180.1]]);
    assert.ok(A.has(across, -16.95, 179.95));
    assert.ok(A.has(across, -16.95, -179.95));
    assert.ok(!A.has(across, -16.95, -179.8));
    var bounds = A.bounds(across);
    assert.ok(Math.abs(bounds.west - 179.9) < 1e-6 && Math.abs(bounds.east - 180.1) < 1e-6,
              JSON.stringify(bounds));
    var line = A.encode("l", [[0, 179.999, 0, -179.999]], 100);
    // (A line given across it goes the long way round: the short way is as drawn,
    // past 180.)
    var short = A.encode("l", [[0, 179.999, 0, 180.001]], 100);
    assert.ok(A.has(short, 0, -179.9995) && A.has(short, 0, 179.9995));
    assert.ok(A.read(line));
});

test("an area in words, its bounds, and a line simplified", function () {
    assert.strictEqual(A.label(BOX, ""), "a rectangle on the map");
    assert.strictEqual(A.label(SHAPE, ""), "drawn on the map");
    assert.strictEqual(A.label(SHAPE, "Test Park"), "Test Park");
    assert.strictEqual(A.label(ROAD, "Test Road"), "Test Road, 50 m either side");
    assert.strictEqual(A.label(ROAD, ""), "along a line, 50 m either side");
    assert.strictEqual(A.label(SPOT, "Test Museum"), "within 100 m of Test Museum");
    assert.strictEqual(A.label(A.encode("l", [[45, 10]], 1500), "\u0001x\n"),
                       "within 1.5 km of x");
    assert.strictEqual(A.label("nope", "x"), "");
    assert.strictEqual(A.name("  Test\tRoad  "), "Test Road");
    assert.strictEqual(A.name(new Array(300).join("x")).length, A.NAME_LENGTH);
    var bounds = A.bounds(BOX);
    assert.ok(Math.abs(bounds.south - 44.99) < 1e-6 && Math.abs(bounds.north - 45.01) < 1e-6);
    // A line's metres around it.
    bounds = A.bounds(ROAD);
    assert.ok(bounds.north > 45.0004 && bounds.south < 44.9996, JSON.stringify(bounds));
    // Points within the tolerance of the line through the others go.
    assert.deepStrictEqual(A.simplify([0, 0, 1, 0.01, 2, 0, 3, 1], 0.1), [0, 0, 2, 0, 3, 1]);
    assert.deepStrictEqual(A.simplify([0, 0, 1, 1], 5), [0, 0, 1, 1]);
});

/* ---------- The area option of the search ------------------------------------------- */

function recordsNear() {
    return [
        {i: 1, l: "lib", t: "Harbour", g: ["harbour"], dk: "2024-05-01", ds: 1, gp: 1},
        {i: 2, l: "lib", t: "Canal", g: ["canal"], dk: "2024-05-02", ds: 1, gp: 1},
        {i: 3, l: "lib", t: "Fountain", g: ["fountain"], dk: "2024-05-03", ds: 1, pe: 1},
        {i: 4, l: "lib", t: "Market", g: ["market"], dk: "2024-05-04", ds: 1, pe: 1},
        {i: 5, l: "lib", t: "Drive", g: ["road"], dk: "2024-05-05", ds: 1, mv: 1, ty: "video",
         gp: 1},
        {i: 6, l: "lib", t: "Desert", g: ["desert"], dk: "2024-05-06", ds: 1, gp: 1},
        {i: 7, l: "lib", t: "Unplaced", g: ["harbour"], dk: "2024-05-07", ds: 1}
    ];
}

// Where they were taken: 1 and 2 by the road, 3 an estimate of 0.3 km at it, 4 one
// of 3 km, 5 a clip whose own position is away but whose route crosses it, 6 far.
var POINTS = [{id: 1, lat: 45.0001, lon: 10.0}, {id: 2, lat: 45.0, lon: 10.005},
              {id: 3, lat: 45.0, lon: 10.0, e: 1, r: 0.3},
              {id: 4, lat: 45.0, lon: 10.0, e: 1, r: 3},
              {id: 5, lat: 45.05, lon: 10.0}, {id: 6, lat: -30, lon: 140}];
// (tracks.encode: tenths of seconds, lat and lon in 1/100000 degree, speed.)
var ROUTES = [{d: 5, l: "lib", c: [{i: 5, o: 0, p: JSON.stringify(
    [[0, 4505000, 1000000, 10], [100, -5000, 0, 10], [100, -5000, 0, 10]])}]}];

function ids(index, query, options) {
    return SearchCore.search(index, query, options).items.slice().sort();
}

test("an area finds the pictures taken in it: positions, routes, estimates deep enough",
     function () {
         var index = SearchCore.prepare(recordsNear());
         // Without the places nothing is found yet (and the answer says so).
         var pending = SearchCore.search(index, "", {area: ROAD});
         assert.strictEqual(pending.total, 0);
         assert.strictEqual(pending.areaPending, true);
         assert.strictEqual(index.setPlaces(POINTS, ROUTES), 6);
         var found = SearchCore.search(index, "", {area: ROAD});
         assert.strictEqual(found.areaPending, false);
         // 1 and 2 by the road; 5 by its route; 3, an estimate of 300 m, would have
         // to be 150 m deep in a road 50 m wide: no; 4 neither.
         assert.deepStrictEqual(found.items.slice().sort(), [1, 2, 5]);
         // In the rectangle, 1.1 km deep at most: the estimate of 0.3 km is (150 m
         // deep), the one of 3 km is not (1.5 km).
         assert.deepStrictEqual(ids(index, "", {area: BOX}), [1, 2, 3, 5]);
         // Without the routes, the clip goes by its own position (away).
         index.setPlaces(POINTS, null);
         assert.deepStrictEqual(ids(index, "", {area: ROAD}), [1, 2]);
         // With words and filters too.
         index.setPlaces(POINTS, ROUTES);
         assert.deepStrictEqual(ids(index, "harbour", {area: BOX}), [1]);
         assert.deepStrictEqual(ids(index, "", {area: BOX, filters: {type: ["video"]}}), [5]);
         // The area read already works the same.
         assert.deepStrictEqual(ids(index, "", {area: A.read(BOX)}), [1, 2, 3, 5]);
     });

test("the owner's places: a position set exact, an estimate accepted or a town by its radius",
     function () {
         var records = recordsNear().map(function (rec) {
             var copy = {};
             Object.keys(rec).forEach(function (key) {
                 copy[key] = rec[key];
             });
             delete copy.pe;
             delete copy.gp;
             copy.ps = "o";
             return copy;
         });
         var index = SearchCore.prepare(records);
         // 1 a position set; 3 an estimate of 0.3 km accepted; 4 a town chosen (its
         // middle, 5 km off), all at the road.
         index.setPlaces([{id: 1, lat: 45.0001, lon: 10.0, o: 1},
                          {id: 3, lat: 45.0, lon: 10.0, o: 1, r: 0.3},
                          {id: 4, lat: 45.0, lon: 10.0, o: 1, r: 5}], null);
         assert.deepStrictEqual(ids(index, "", {area: ROAD}), [1]);
         assert.deepStrictEqual(ids(index, "", {area: BOX}), [1, 3]);
     });

test("an area that cannot be read is left out, with a warning", function () {
    var index = SearchCore.prepare(recordsNear());
    index.setPlaces(POINTS, ROUTES);
    var found = SearchCore.search(index, "", {area: "l50.!!"});
    assert.strictEqual(found.total, 7);
    assert.ok(/area on the map could not be read/.test(found.warnings.join(" ")),
              found.warnings.join(" "));
});

test("a smart album keeps its area: album: finds the pictures of its search in it",
     function () {
         var index = SearchCore.prepare(recordsNear());
         var albums = [{id: 3, name: "By the road", kind: "smart", query: "",
                        options: {area: ROAD, aname: "Test Road"}},
                       {id: 4, name: "Harbours here", kind: "smart", query: "harbour",
                        options: {area: BOX}}];
         var pending = SearchCore.search(index, "album:\"By the road\"", {albums: albums});
         assert.strictEqual(pending.total, 0);
         assert.strictEqual(pending.areaPending, true);
         index.setPlaces(POINTS, ROUTES);
         assert.deepStrictEqual(ids(index, "album:\"By the road\"", {albums: albums}),
                                [1, 2, 5]);
         assert.deepStrictEqual(ids(index, "album:\"Harbours here\"", {albums: albums}), [1]);
         // (An album's area and the search's own together: 2 is 400 m from the spot.)
         assert.deepStrictEqual(ids(index, "album:\"By the road\"",
                                    {albums: albums, area: SPOT}), [1, 5]);
     });

/* ---------- The area in the address -------------------------------------------------- */

test("the address keeps an area and its name, and only an area of the right form",
     function () {
         var state = SITE.searchState.read("?q=x&area=" + ROAD + "&aname=Test%20Road");
         assert.strictEqual(state.area, ROAD);
         assert.strictEqual(state.aname, "Test Road");
         var query = SITE.searchState.params(state);
         assert.ok(query.indexOf("area=" + ROAD) >= 0, query);
         assert.ok(query.indexOf("aname=Test+Road") >= 0, query);
         // A name without an area, or an area of no known form, is left out.
         assert.strictEqual(SITE.searchState.read("?aname=Test").aname, "");
         assert.strictEqual(SITE.searchState.read("?area=z.AAAA&aname=Test").area, "");
         assert.strictEqual(SITE.searchState.read("?area=" + ROAD + "&aname=a%0Ab").aname,
                            "a b");
         assert.strictEqual(SITE.searchState.params({area: "nope", aname: "x"}), "");
         assert.strictEqual(SITE.searchState.params({aname: "x"}), "");
     });

/* ---------- The search page ------------------------------------------------------------ */

function response(status, body) {
    var text = JSON.stringify(body);
    return Promise.resolve({
        ok: status >= 200 && status < 300,
        status: status,
        headers: {get: function () {
            return null;
        }},
        body: null,
        json: function () {
            return Promise.resolve().then(function () {
                return JSON.parse(text);
            });
        }
    });
}

function storage() {
    var items = {};
    return {
        getItem: function (key) {
            return Object.prototype.hasOwnProperty.call(items, key) ? items[key] : null;
        },
        setItem: function (key, value) {
            items[key] = String(value);
        },
        removeItem: function (key) {
            delete items[key];
        }
    };
}

/* The admin's search page on the server at an address (searchText: its ?...
   part); capabilities: of the page (map: false is a site without locations).
   page.log: the addresses asked for, and [[admin route, body]] sent. */
function searchPage(searchText, capabilities, failPlaces) {
    var log = {requests: [], sent: [], urls: []};
    var document = FAKE.makeDocument();
    document.createElementNS = function (ns, tag) {
        return document.createElement(tag);
    };
    var config = {mode: "server", page: "search", api: "/api", tiles: "/tile",
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: capabilities || {admin: true, static: false}};
    FAKE.parseBody(document, FAKE.PAGE, config);
    var albums = [{id: 9, name: "By the road", kind: "smart", query: "",
                   options: {area: ROAD, aname: "Test Road"}}];
    function fetch(url, init) {
        log.requests.push(String(url));
        var plain = String(url).split("?")[0];
        var body = init && init.body ? JSON.parse(init.body) : null;
        if (plain === "/api/index") {
            return response(200, {version: 1, libraries: [{id: "lib", name: "Example"}],
                                  albums: albums, records: recordsNear()});
        }
        if (plain === "/api/locations") {
            return failPlaces ? response(500, {error: "down"}) : response(200, POINTS);
        }
        if (plain === "/api/routes") {
            return response(200, {routes: ROUTES});
        }
        var admin = /^\/api\/admin(\/.*)$/.exec(plain);
        if (admin && admin[1] === "/albums" && body) {
            log.sent.push([admin[1], body]);
            return response(201, {album: {id: 50, name: body.name, kind: body.kind,
                                          cover: null, query: body.query || "",
                                          options: body.options || {}}});
        }
        return response(404, {error: "not here"});
    }
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: false}, clipboard: null},
        location: {search: searchText || "", hash: "", pathname: "/", protocol: "http:",
                   href: "http://127.0.0.1:5000/" + (searchText || ""),
                   origin: "http://127.0.0.1:5000"},
        history: {state: null, scrollRestoration: "auto", pushState: function (state, title,
                                                                             url) {
            this.state = state;
            log.urls.push(String(url));
        }, replaceState: function (state, title, url) {
            this.state = state;
            log.urls.push(String(url));
        }, back: function () {}},
        localStorage: storage(),
        sessionStorage: storage(),
        innerWidth: 1400,
        innerHeight: 900,
        scrollY: 0,
        devicePixelRatio: 1,
        scrollTo: function () {},
        scrollBy: function () {},
        matchMedia: function () {
            return {matches: false, addEventListener: function () {}, addListener: function () {}};
        },
        requestAnimationFrame: function (callback) {
            return setTimeout(callback, 0);
        },
        cancelAnimationFrame: clearTimeout,
        getComputedStyle: function () {
            return {getPropertyValue: function () {
                return "";
            }};
        },
        setTimeout: setTimeout,
        clearTimeout: clearTimeout,
        setInterval: setInterval,
        clearInterval: clearInterval,
        console: {log: function () {}, warn: function () {}, error: function () {}},
        URLSearchParams: URLSearchParams,
        URL: URL,
        fetch: fetch
    };
    context.window = context;
    context.addEventListener = function () {};
    context.removeEventListener = function () {};
    vm.createContext(context);
    FAKE.SCRIPTS.forEach(function (script) {
        vm.runInContext(script.code, context, {filename: script.name});
    });
    return {context: context, document: document, log: log};
}

function byId(page, id) {
    return page.document.getElementById(id);
}

function counted(page) {
    return FAKE.waitFor(function () {
        var text = byId(page, "result-count").textContent;
        return /\d+ pictures?/.test(text) ? text : null;
    });
}

function chips(page) {
    return byId(page, "active-filters").querySelectorAll(".filter-chip").map(function (chip) {
        return chip.textContent;
    });
}

test("the search page searches the area: the places come, a chip names it, x takes it away",
     function () {
         var page = searchPage("?area=" + ROAD + "&aname=Test%20Road");
         return FAKE.waitFor(function () {
             return /3 pictures/.test(byId(page, "result-count").textContent);
         }).then(function () {
             assert.ok(page.log.requests.indexOf("/api/locations") >= 0, page.log.requests);
             assert.ok(page.log.requests.indexOf("/api/routes") >= 0, page.log.requests);
             assert.deepStrictEqual(chips(page), ["Area: Test Road, 50 m either side\u00d7"]);
             byId(page, "active-filters").querySelector(".filter-chip").click();
             return counted(page);
         }).then(function (text) {
             assert.ok(/7 pictures/.test(text), text);
             var last = page.log.urls[page.log.urls.length - 1];
             assert.ok(last.indexOf("area=") < 0 && last.indexOf("aname=") < 0, last);
         });
     });

test("without an area the places are not loaded; a smart album with one loads them",
     function () {
         var page = searchPage("?q=harbour");
         return counted(page).then(function () {
             assert.ok(page.log.requests.indexOf("/api/locations") < 0, page.log.requests);
             FAKE.search(page, "album:\"By the road\"");
             return FAKE.waitFor(function () {
                 return /3 pictures/.test(byId(page, "result-count").textContent);
             });
         }).then(function () {
             assert.ok(page.log.requests.indexOf("/api/locations") >= 0, page.log.requests);
         });
     });

test("Save as album keeps the area and its name", function () {
    var page = searchPage("?q=harbour&area=" + BOX);
    return FAKE.waitFor(function () {
        return /1 picture/.test(byId(page, "result-count").textContent) &&
            !byId(page, "save-search").hidden;
    }).then(function () {
        byId(page, "save-search").click();
        return FAKE.settle(5);
    }).then(function () {
        var boxes = page.document.querySelectorAll("dialog");
        var box = boxes[boxes.length - 1];
        assert.ok(/the area of the map \(a rectangle on the map\)/.test(box.textContent),
                  box.textContent);
        var buttons = box.querySelectorAll(".modal-foot button");
        buttons[buttons.length - 1].click();
        return FAKE.waitFor(function () {
            return page.log.sent.length === 1;
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[0][1].options, {area: BOX});
    });
});

test("an area alone may be saved; a site without locations leaves the area out", function () {
    var page = searchPage("?area=" + SPOT + "&aname=Test%20Museum");
    var plain = null;
    return FAKE.waitFor(function () {
        // (1 by its position, 5 by its route; the estimate of 300 m is too vague.)
        return /2 pictures/.test(byId(page, "result-count").textContent) &&
            !byId(page, "save-search").hidden;
    }).then(function () {
        // Saved as an album: its name is offered as the album's (no words to offer).
        byId(page, "save-search").click();
        return FAKE.settle(5);
    }).then(function () {
        var boxes = page.document.querySelectorAll("dialog");
        var name = boxes[boxes.length - 1].querySelector("input.input");
        assert.strictEqual(name.value, "Test Museum");
        plain = searchPage("?area=" + SPOT, {admin: false, static: false, map: false});
        return counted(plain);
    }).then(function (text) {
        assert.ok(/7 pictures/.test(text), text);
        assert.deepStrictEqual(chips(plain), []);
        assert.ok(/does not publish where the pictures were taken/.test(
            byId(plain, "query-notes").textContent), byId(plain, "query-notes").textContent);
        assert.ok(plain.log.requests.indexOf("/api/locations") < 0);
    });
});

test("places that cannot be loaded: a note, and asked again only with the next search",
     function () {
         var page = searchPage("?area=" + BOX, null, true);
         function asked() {
             return page.log.requests.filter(function (url) {
                 return url === "/api/locations";
             }).length;
         }
         return FAKE.waitFor(function () {
             return /could not be loaded/.test(byId(page, "query-notes").textContent);
         }).then(function () {
             return FAKE.settle(50);
         }).then(function () {
             assert.strictEqual(asked(), 1);
             FAKE.search(page, "harbour");
             return FAKE.waitFor(function () {
                 return asked() === 2;
             });
         });
     });

test("nothing in the area: the page says the search finds pictures elsewhere", function () {
    var page = searchPage("?q=desert&area=" + BOX);
    return FAKE.waitFor(function () {
        return /none was taken in the area/.test(byId(page, "empty").textContent);
    }).then(function () {
        var button = byId(page, "empty").querySelector("button");
        assert.strictEqual(button.textContent, "Search everywhere");
        button.click();
        return counted(page);
    }).then(function (text) {
        assert.ok(/1 picture/.test(text), text);
    });
});

test("nothing in the area, and what matches has no position: the page says so", function () {
    // (A place named without a position -- one its file names -- is in no area.)
    var page = searchPage("?q=unplaced&area=" + BOX);
    return FAKE.waitFor(function () {
        return /in no area of the map/.test(byId(page, "empty").textContent);
    }).then(function () {
        var text = byId(page, "empty").textContent;
        assert.ok(new RegExp("The picture that matches \u201cunplaced\u201d has no position, " +
                             "so it is in no area of the map").test(text), text);
        assert.strictEqual(byId(page, "empty").querySelector("button").textContent,
                           "Search everywhere");
    });
});

/* ---------- The map page --------------------------------------------------------------- */

var MAP_PAGE = fs.readFileSync(path.join(ROOT, "templates", "map.html"), "utf8");
var MAP_SCRIPTS = ["site.js", "shell.js", "search-core.js", "streets.js", "routes.js",
                   "viewer.js", "map.js"].map(function (name) {
    return {name: name, code: fs.readFileSync(path.join(STATIC, name), "utf8")};
});

/* The admin's map page at an address. page.urls: the addresses it wrote;
   page.fire(type, event): an event of the window (mousemove, mouseup);
   page.fills: the fill() calls of its canvas ([rule]). options: streets (the
   page has street maps), beforeMap(context) (called before map.js runs: a
   made-up STREETS.Picker, see fakePicker), coarse (a touch screen). */
function mapPage(searchText, options) {
    options = options || {};
    var urls = [];
    var fills = [];
    var windowListeners = {};
    var document = FAKE.makeDocument();
    var config = {mode: "server", page: "map", api: "/api", tiles: "/tile", tilesMax: 8,
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: {admin: true, static: false}};
    if (options.streets) {
        config.streets = {url: "maps/", v: ""};
    }
    FAKE.parseBody(document, MAP_PAGE, config);
    var canvas = document.getElementById("map-canvas");
    canvas.getContext = function () {
        return new Proxy({}, {get: function (target, name) {
            if (name === "measureText") {
                return function (text) {
                    return {width: String(text).length * 6};
                };
            }
            if (name === "fill") {
                return function (rule) {
                    fills.push([rule]);
                };
            }
            if (!(name in target)) {
                target[name] = function () {};
            }
            return target[name];
        }, set: function () {
            return true;
        }});
    };
    canvas.getBoundingClientRect = function () {
        return {top: 0, left: 0, right: 1000, bottom: 700, width: 1000, height: 700};
    };
    function fetch(url) {
        var plain = String(url).split("?")[0];
        if (plain === "/api/index") {
            return response(200, {version: 1, libraries: [{id: "lib", name: "Example"}],
                                  albums: [], records: recordsNear()});
        }
        if (plain === "/api/locations") {
            return response(200, POINTS);
        }
        if (plain === "/api/routes") {
            return response(200, {routes: ROUTES});
        }
        return response(404, null);
    }
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: false}, clipboard: null,
                    maxTouchPoints: 0},
        location: {search: searchText || "", hash: "", pathname: "/map", protocol: "http:",
                   href: "http://127.0.0.1:5000/map" + (searchText || ""),
                   origin: "http://127.0.0.1:5000"},
        history: {state: null, scrollRestoration: "auto", pushState: function (state, title,
                                                                             url) {
            this.state = state;
            urls.push(String(url));
            context.location.search = String(url).replace(/^[^?#]*/, "").replace(/#.*$/, "");
        }, replaceState: function (state, title, url) {
            this.state = state;
            urls.push(String(url));
            context.location.search = String(url).replace(/^[^?#]*/, "").replace(/#.*$/, "");
        }, back: function () {}},
        localStorage: storage(),
        sessionStorage: storage(),
        innerWidth: 1000,
        innerHeight: 760,
        scrollY: 0,
        devicePixelRatio: 1,
        scrollTo: function () {},
        matchMedia: function (query) {
            return {matches: Boolean(options.coarse) && /coarse/.test(String(query)),
                    addEventListener: function () {}, addListener: function () {},
                    removeEventListener: function () {}, removeListener: function () {}};
        },
        requestAnimationFrame: function (callback) {
            return setTimeout(callback, 0);
        },
        cancelAnimationFrame: clearTimeout,
        getComputedStyle: function () {
            return {getPropertyValue: function () {
                return "";
            }};
        },
        setTimeout: setTimeout,
        clearTimeout: clearTimeout,
        setInterval: setInterval,
        clearInterval: clearInterval,
        console: {log: function () {}, warn: function () {}, error: function () {}},
        URLSearchParams: URLSearchParams,
        URL: URL,
        Image: function () {
            this.complete = false;
        },
        fetch: fetch
    };
    context.window = context;
    context.addEventListener = function (type, listener) {
        (windowListeners[type] = windowListeners[type] || []).push(listener);
    };
    context.removeEventListener = function () {};
    vm.createContext(context);
    MAP_SCRIPTS.forEach(function (script) {
        if (script.name === "map.js" && options.beforeMap) {
            options.beforeMap(context);
        }
        vm.runInContext(script.code, context, {filename: script.name});
    });
    return {
        document: document,
        urls: urls,
        fills: fills,
        context: context,
        last: function () {
            return new URLSearchParams(context.location.search);
        },
        fire: function (type, event) {
            event.type = type;
            event.preventDefault = function () {};
            (windowListeners[type] || []).forEach(function (listener) {
                listener(event);
            });
        }
    };
}

function status(page) {
    return page.document.getElementById("map-status").textContent;
}

function mapChip(page) {
    return page.document.getElementById("map-area-chip").textContent;
}

test("the map shows the area of the address: its pictures, its chip, x takes it away",
     function () {
         var page = mapPage("?area=" + ROAD + "&aname=Test%20Road");
         return FAKE.waitFor(function () {
             return /3 pictures in this area/.test(status(page));
         }).then(function () {
             assert.strictEqual(mapChip(page), "Area: Test Road, 50 m either side\u00d7");
             assert.strictEqual(page.document.getElementById("map-area-button").textContent,
                                "Change the area");
             // (Back to search keeps it.)
             var back = page.document.getElementById("map-back").href;
             assert.ok(String(back).indexOf("area=" + ROAD) >= 0, back);
             page.document.getElementById("map-area-chip").querySelector("button").click();
             return FAKE.waitFor(function () {
                 return /with a location/.test(status(page));
             });
         }).then(function () {
             assert.strictEqual(page.last().get("area"), null);
             assert.strictEqual(page.document.getElementById("map-area-chip").hidden, true);
         });
     });

test("an area with nothing in it says so, without a card over the map", function () {
    var far = A.encode("b", [[60, 60, 60.1, 60.1]]);
    var page = mapPage("?area=" + far);
    return FAKE.waitFor(function () {
        return /No pictures in this area/.test(status(page));
    }).then(function () {
        assert.strictEqual(page.document.getElementById("map-message").hidden, true);
    });
});

test("an area that holds none of what matches, which has no position, says so", function () {
    var page = mapPage("?q=unplaced&area=" + BOX);
    return FAKE.waitFor(function () {
        return /in no area of the map/.test(status(page));
    }).then(function () {
        assert.ok(/^The picture that matches has no position, so it is in no area of the map/
            .test(status(page)), status(page));
        assert.strictEqual(page.document.getElementById("map-message").hidden, true);
    });
});

test("This view searches the rectangle the map shows; Esc closes the tools", function () {
    var page = mapPage("?view=45,10,12");
    var tools = null;
    return FAKE.waitFor(function () {
        return /with a location/.test(status(page));
    }).then(function () {
        page.document.getElementById("map-area-button").click();
        tools = page.document.getElementById("map-area-tools");
        assert.strictEqual(tools.hidden, false);
        assert.strictEqual(page.document.getElementById("map-area-button")
            .getAttribute("aria-expanded"), "true");
        page.document.getElementById("map-area-view").click();
        return FAKE.waitFor(function () {
            return /in this area/.test(status(page));
        });
    }).then(function () {
        var area = A.read(page.last().get("area"));
        assert.strictEqual(area.kind, "b");
        assert.ok(A.has(area, 45, 10) && !A.has(area, 45.2, 10), area.text);
        assert.strictEqual(tools.hidden, true);
        // (All but the one far away; the estimates are deep enough inside.)
        assert.ok(/5 pictures in this area \(2 estimated\)/.test(status(page)), status(page));
        // Esc: the tool chosen first, then the tools.
        page.document.getElementById("map-area-button").click();
        tools.querySelector("[data-mode='box']").click();
        assert.strictEqual(tools.querySelector("[data-mode='box']")
            .getAttribute("aria-pressed"), "true");
        page.document.dispatchEvent({type: "keydown", key: "Escape",
                                     target: page.document.body});
        assert.strictEqual(tools.querySelector("[data-mode='box']")
            .getAttribute("aria-pressed"), "false");
        assert.strictEqual(tools.hidden, false);
        page.document.dispatchEvent({type: "keydown", key: "Escape",
                                     target: page.document.body});
        assert.strictEqual(tools.hidden, true);
    });
});

test("a rectangle and a shape drawn with the mouse become the area", function () {
    var page = mapPage("?view=45,10,12");
    var canvas = null;
    function drag(points) {
        canvas.dispatchEvent({type: "mousedown", button: 0, clientX: points[0][0],
                              clientY: points[0][1]});
        points.slice(1).forEach(function (point) {
            page.fire("mousemove", {clientX: point[0], clientY: point[1], target: canvas});
        });
        var last = points[points.length - 1];
        page.fire("mouseup", {clientX: last[0], clientY: last[1], target: canvas});
    }
    return FAKE.waitFor(function () {
        return /with a location/.test(status(page));
    }).then(function () {
        canvas = page.document.getElementById("map-canvas");
        page.document.getElementById("map-area-button").click();
        var tools = page.document.getElementById("map-area-tools");
        tools.querySelector("[data-mode='box']").click();
        // A drag of a few pixels is no area: the map stays as it was.
        drag([[500, 350], [503, 352]]);
        assert.strictEqual(page.last().get("area"), null);
        assert.ok(/too small/.test(page.document.getElementById("map-area-hint").textContent));
        // Around the middle (45 N 10 E at zoom 12: about 38 m a pixel).
        drag([[480, 330], [500, 350], [520, 370]]);
        return FAKE.waitFor(function () {
            return page.last().get("area");
        });
    }).then(function (text) {
        var area = A.read(text);
        assert.strictEqual(area.kind, "b");
        assert.ok(A.has(area, 45, 10), text);
        assert.ok(!A.has(area, 45.01, 10), text);
        assert.strictEqual(page.document.getElementById("map-area-tools").hidden, true);
        assert.strictEqual(mapChip(page), "Area: a rectangle on the map\u00d7");
        // A shape drawn around the middle.
        page.document.getElementById("map-area-button").click();
        page.document.getElementById("map-area-tools").querySelector("[data-mode='draw']")
            .click();
        var ring = [];
        for (var k = 0; k <= 36; k++) {
            ring.push([500 + 40 * Math.cos(k / 36 * 2 * Math.PI),
                       350 + 40 * Math.sin(k / 36 * 2 * Math.PI)]);
        }
        drag(ring);
        return FAKE.waitFor(function () {
            var value = page.last().get("area");
            return value && value.charAt(0) === "p" ? value : null;
        });
    }).then(function (text) {
        var area = A.read(text);
        assert.ok(A.has(area, 45, 10), text);
        assert.ok(!A.has(area, 45, 10.03), text);
        assert.ok(area.parts[0].length / 2 < 37, "simplified: " + area.parts[0].length / 2);
        assert.strictEqual(mapChip(page), "Area: drawn on the map\u00d7");
    });
});

test("Street or place needs the street maps: without them it is not offered", function () {
    var page = mapPage("?view=45,10,14");
    return FAKE.waitFor(function () {
        return /with a location/.test(status(page));
    }).then(function () {
        page.document.getElementById("map-area-button").click();
        var pick = page.document.getElementById("map-area-tools")
            .querySelector("[data-mode='pick']");
        assert.strictEqual(pick.hidden, true);
        var hint = page.document.getElementById("map-area-hint").textContent;
        assert.ok(!/pick a street or a place of the street maps/.test(hint), hint);
        // (The admin on the server is told where street maps come from.)
        assert.ok(/Street maps \(Manage, Street maps\)/.test(hint), hint);
    });
});

test("a drawn line that crosses itself is a lasso: what its first loop holds, drawn even-odd",
     function () {
         var page = mapPage("?view=45,10,12");
         var canvas = null;
         return FAKE.waitFor(function () {
             return /with a location/.test(status(page));
         }).then(function () {
             canvas = page.document.getElementById("map-canvas");
             page.document.getElementById("map-area-button").click();
             page.document.getElementById("map-area-tools").querySelector("[data-mode='draw']")
                 .click();
             // Once round the middle, back past where it started, and on to the east.
             var points = [];
             for (var k = 0; k <= 40; k++) {
                 points.push([500 + 40 * Math.cos(k / 36 * 2 * Math.PI),
                              350 + 40 * Math.sin(k / 36 * 2 * Math.PI)]);
             }
             points.push([650, 340], [650, 250]);
             canvas.dispatchEvent({type: "mousedown", button: 0, clientX: points[0][0],
                                   clientY: points[0][1]});
             points.slice(1).forEach(function (point) {
                 page.fire("mousemove", {clientX: point[0], clientY: point[1],
                                         target: canvas});
             });
             // (The sketch is drawn even-odd, as the area is searched.)
             return FAKE.waitFor(function () {
                 return page.fills.some(function (call) {
                     return call[0] === "evenodd";
                 });
             }).then(function () {
                 var last = points[points.length - 1];
                 page.fire("mouseup", {clientX: last[0], clientY: last[1], target: canvas});
                 return FAKE.waitFor(function () {
                     return page.last().get("area");
                 });
             });
         }).then(function (text) {
             var area = A.read(text);
             assert.ok(A.has(area, 45, 10), text);
             // (East of the loop, where the tail went: not in it. 1 px is about 0.00034
             // degrees of longitude at zoom 12.)
             var bounds = A.bounds(area);
             assert.ok(bounds.east < 10.02, JSON.stringify(bounds));
             assert.ok(page.fills.some(function (call) {
                 return call[0] === "evenodd";
             }), JSON.stringify(page.fills));
         });
     });

test("an area set from the tools with the keyboard leaves the focus on its chip", function () {
    var page = mapPage("?view=45,10,12");
    return FAKE.waitFor(function () {
        return /with a location/.test(status(page));
    }).then(function () {
        page.document.getElementById("map-area-button").click();
        var thisView = page.document.getElementById("map-area-view");
        thisView.focus();
        thisView.click();
        return FAKE.waitFor(function () {
            return /in this area/.test(status(page));
        });
    }).then(function () {
        var chip = page.document.getElementById("map-area-chip").querySelector("button");
        assert.strictEqual(page.document.activeElement, chip);
    });
});

test("with words too, the status counts the matching pictures in the area", function () {
    var page = mapPage("?q=harbour&area=" + BOX);
    return FAKE.waitFor(function () {
        return /in this area/.test(status(page));
    }).then(function () {
        assert.ok(/^1 matching picture in this area/.test(status(page)), status(page));
    });
});

/* A made-up STREETS.Picker for the map page: names (the list of the view, once
   becomeReady() has been called; before, inView answers "wait"), here (what is at
   any place), gathered (the names read in full). */
function fakePicker(context, names, here) {
    var made = {waiters: [], ready: false, gathered: []};
    function Picker() {}
    Picker.prototype.ready = function () {
        return made.ready;
    };
    Picker.prototype.whenReady = function (callback) {
        if (made.ready) {
            callback(true);
        } else {
            made.waiters.push(callback);
        }
    };
    Picker.prototype.inView = function (box, zoom, callback) {
        if (!made.ready) {
            callback(null, "wait");
            return;
        }
        callback(names.slice());
    };
    Picker.prototype.at = function (lat, lon, metres, callback) {
        callback(here.slice());
    };
    Picker.prototype.gather = function (item, callback) {
        made.gathered.push(item.name);
        callback({kind: item.kind, name: item.name, what: item.what, type: item.type,
                  parts: [[45, 9.99, 45, 10.01]], cut: false});
    };
    made.becomeReady = function () {
        made.ready = true;
        made.waiters.splice(0).forEach(function (callback) {
            callback(true);
        });
    };
    context.STREETS.Picker = Picker;
    return made;
}

var NAMES = [{kind: "street", name: "Test Road", what: "street", key: "15/1/1"},
             {kind: "place", name: "Dog Run", what: "dog park", type: "dog_park", key: "15/1/1",
              near: "north-west", point: [45.001, 9.999]},
             {kind: "place", name: "Dog Run", what: "dog park", type: "dog_park", key: "15/1/1",
              near: "south-east", point: [44.999, 10.001]}];

function pickPage(options) {
    var made = null;
    var page = mapPage("?view=45,10,15", Object.assign({streets: true, beforeMap: function (
        context) {
        made = fakePicker(context, NAMES, NAMES.slice(1));
    }}, options || {}));
    page.picker = function () {
        return made;
    };
    return page;
}

test("Street or place lists the view's names once the street maps are read, not none before",
     function () {
         var page = pickPage();
         var list = null;
         return FAKE.waitFor(function () {
             return /with a location/.test(status(page));
         }).then(function () {
             page.document.getElementById("map-area-button").click();
             page.document.getElementById("map-area-tools").querySelector("[data-mode='pick']")
                 .click();
             list = page.document.getElementById("map-area-list");
             assert.ok(/Reading the street maps/.test(list.textContent), list.textContent);
             var input = page.document.getElementById("map-area-input");
             input.value = "dog";
             input.dispatchEvent({type: "input"});
             assert.ok(!/is called that/.test(list.textContent), list.textContent);
             page.picker().becomeReady();
             return FAKE.waitFor(function () {
                 return /Dog Run/.test(list.textContent);
             });
         }).then(function () {
             // Two of one name are told apart by where they are in the view.
             assert.ok(/dog park \(north-west\)/.test(list.textContent), list.textContent);
             assert.ok(/dog park \(south-east\)/.test(list.textContent), list.textContent);
         });
     });

test("a pick before the street maps are read waits for them; Which one? takes arrow keys",
     function () {
         var page = pickPage();
         var canvas = null;
         var menu = null;
         return FAKE.waitFor(function () {
             return /with a location/.test(status(page));
         }).then(function () {
             canvas = page.document.getElementById("map-canvas");
             page.document.getElementById("map-area-button").click();
             page.document.getElementById("map-area-tools").querySelector("[data-mode='pick']")
                 .click();
             canvas.dispatchEvent({type: "mousedown", button: 0, clientX: 500, clientY: 350});
             page.fire("mouseup", {clientX: 500, clientY: 350, target: canvas});
             var hint = page.document.getElementById("map-area-hint").textContent;
             assert.ok(/Reading the street maps/.test(hint), hint);
             assert.ok(!/Nothing with a name/.test(hint), hint);
             page.picker().becomeReady();
             menu = page.document.getElementById("map-area-menu");
             return FAKE.waitFor(function () {
                 return !menu.hidden;
             });
         }).then(function () {
             var items = menu.querySelectorAll("[role='menuitem']");
             assert.strictEqual(items.length, 2);
             assert.strictEqual(page.document.activeElement, items[0]);
             menu.dispatchEvent({type: "keydown", key: "ArrowDown", target: items[0]});
             assert.strictEqual(page.document.activeElement, items[1]);
             menu.dispatchEvent({type: "keydown", key: "ArrowDown", target: items[1]});
             assert.strictEqual(page.document.activeElement, items[0]);
             menu.dispatchEvent({type: "keydown", key: "End", target: items[0]});
             assert.strictEqual(page.document.activeElement, items[1]);
             menu.dispatchEvent({type: "keydown", key: "Home", target: items[1]});
             assert.strictEqual(page.document.activeElement, items[0]);
             assert.ok(/dog park \(north-west\)/.test(items[0].textContent),
                       items[0].textContent);
             // Chosen with the keyboard: the area is set, the focus on its chip.
             items[1].click();
             return FAKE.waitFor(function () {
                 return page.last().get("area");
             });
         }).then(function () {
             assert.deepStrictEqual(page.picker().gathered, ["Dog Run"]);
             assert.strictEqual(page.document.activeElement,
                                page.document.getElementById("map-area-chip")
                                    .querySelector("button"));
             // A place: the circle that holds the pictures named after it (a dog
             // park's reach, 60 m, and half an indoor position's accuracy), which
             // Within shows.
             assert.ok(/^l90\./.test(page.last().get("area")), page.last().get("area"));
             var chip = page.document.getElementById("map-area-chip").textContent;
             assert.ok(/Dog Run, 90 m/.test(chip), chip);
             page.document.getElementById("map-area-button").click();
             assert.strictEqual(String(page.document.getElementById("map-area-metres").value),
                                "90");
         });
     });

test("on a touch screen Street or place leaves the keyboard closed until the box is tapped",
     function () {
         var page = pickPage({coarse: true});
         return FAKE.waitFor(function () {
             return /with a location/.test(status(page));
         }).then(function () {
             page.document.getElementById("map-area-button").click();
             var pick = page.document.getElementById("map-area-tools")
                 .querySelector("[data-mode='pick']");
             var input = page.document.getElementById("map-area-input");
             pick.dispatchEvent({type: "click", button: 0, detail: 1});
             assert.notStrictEqual(page.document.activeElement, input);
             // (From the keyboard -- Enter or Space, detail 0 --: the box.)
             pick.dispatchEvent({type: "click", button: 0, detail: 1});
             pick.dispatchEvent({type: "click", button: 0, detail: 0});
             assert.strictEqual(page.document.activeElement, input);
         });
     });

test("the Within list shows the width of the street searched now", function () {
    var wide = A.encode("l", [[45.0, 9.99, 45.0, 10.01]], 200);
    var odd = A.encode("l", [[45.0, 9.99, 45.0, 10.01]], 75);
    var page = pickPage();
    var metres = null;
    return FAKE.waitFor(function () {
        return /with a location/.test(status(page));
    }).then(function () {
        page.context.history.replaceState(null, "", "/map?view=45,10,15&area=" + wide);
        page = mapPage("?view=45,10,15&area=" + wide + "&aname=Test%20Road",
                       {streets: true, beforeMap: function (context) {
                           fakePicker(context, NAMES, []);
                       }});
        return FAKE.waitFor(function () {
            return /in this area/.test(status(page));
        });
    }).then(function () {
        page.document.getElementById("map-area-button").click();
        metres = page.document.getElementById("map-area-metres");
        assert.strictEqual(String(metres.value), "200");
        page = mapPage("?view=45,10,15&area=" + odd, {streets: true, beforeMap: function (
            context) {
            fakePicker(context, NAMES, []);
        }});
        return FAKE.waitFor(function () {
            return /in this area/.test(status(page));
        });
    }).then(function () {
        page.document.getElementById("map-area-button").click();
        metres = page.document.getElementById("map-area-metres");
        assert.strictEqual(String(metres.value), "75");
        assert.ok(metres.querySelector("[value='75']"), "an option of 75 m");
    });
});

/* ---------- Running -------------------------------------------------------------------- */

var failures = 0;
tests.reduce(function (chain, entry) {
    return chain.then(function () {
        return Promise.resolve().then(entry.body).then(function () {
            console.log("ok - " + entry.name);
        }, function (error) {
            failures += 1;
            console.log("FAIL - " + entry.name);
            console.log(String(error && error.stack || error).split("\n").map(function (line) {
                return "    " + line;
            }).join("\n"));
        });
    });
}, Promise.resolve()).then(function () {
    console.log((tests.length - failures) + " of " + tests.length + " passed");
    process.exit(failures ? 1 : 0);
});
