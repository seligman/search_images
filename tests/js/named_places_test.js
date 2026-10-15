/* Tests for named places in the pages (named_places.py: the record keys np,
   nk, nd and ni): the search finds them by their words and by place:, and
   plain-language search offers one as a place; the viewer's Place line
   names it before the town (a chip that searches for it, with what it is
   and how far); the map's list of a pin names it. The real page scripts
   on the made-up DOM of tests/js/app_meaning_test.js, with a fake fetch()
   for the server's answers. Every place here is made up.

   Run by tests/test_named_places.py, or directly:
       node tests/js/named_places_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var FAKE = require(path.join(__dirname, "app_meaning_test.js"));
var ROOT = path.join(__dirname, "..", "..");
var STATIC = path.join(ROOT, "static");
var SearchCore = require(path.join(STATIC, "search-core.js"));
var VIEWER = require(path.join(STATIC, "viewer.js"));

function records() {
    return [
        {i: 1, l: "lib", t: "Fish in a tank", g: ["fish"], ds: 1, dk: "2021-03-04", gp: 1,
         ci: "Coralport", rg: "North Coast", co: "Examplia", cc: "XA", km: 2,
         np: "Harbour Aquarium", nk: "aquarium", nd: 60},
        {i: 2, l: "lib", t: "Shark", g: ["shark"], ds: 1, dk: "2021-03-04", gp: 1,
         ci: "Coralport", rg: "North Coast", co: "Examplia", cc: "XA", km: 2,
         np: "Harbour Aquarium", nk: "aquarium", nd: 40},
        {i: 3, l: "lib", t: "Lion", g: ["lion"], ds: 1, dk: "2021-03-05", gp: 1,
         np: "City Zoo", nk: "zoo", ni: 1},
        {i: 4, l: "lib", t: "Harbour at dusk", g: ["harbour"], ds: 1, dk: "2021-03-06", gp: 1,
         ci: "Coralport", rg: "North Coast", co: "Examplia", cc: "XA", km: 2},
        {i: 5, l: "lib", t: "Gate", g: ["plane"], ds: 1, dk: "2021-03-07", gp: 1,
         np: "Clarence S. \"Hec\" Field", nk: "aerodrome", ni: 1}
    ];
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

function ids(query) {
    return SearchCore.search(records(), query, {}).items.map(function (item) {
        return item;
    }).sort();
}

/* ---------- The search ------------------------------------------------------ */

test("a named place is found by its words and by place:", function () {
    assert.deepStrictEqual(ids("aquarium"), [1, 2]);
    assert.deepStrictEqual(ids("place:\"harbour aquarium\""), [1, 2]);
    assert.deepStrictEqual(ids("place:zoo"), [3]);
    assert.deepStrictEqual(ids("place:coralport"), [1, 2, 4]);
    assert.deepStrictEqual(ids("-place:aquarium harbour"), [4]);
    assert.deepStrictEqual(SearchCore.highlights("Harbour Aquarium", "aquarium", "np"),
                           [[8, 16]]);
});

test("the query of a named place leaves out what the search cannot take", function () {
    assert.strictEqual(SearchCore.namedPlaceQuery("Harbour Aquarium"),
                       "place:\"harbour aquarium\"");
    var query = SearchCore.namedPlaceQuery("Clarence S. \"Hec\" Field");
    assert.strictEqual(query, "place:\"clarence s hec field\"");
    assert.deepStrictEqual(ids(query), [5]);
    assert.strictEqual(SearchCore.namedPlaceQuery("..."), "");
});

test("plain-language search offers a named place as a place", function () {
    var found = SearchCore.interpret(records(), "fish at harbour aquarium", {});
    assert.strictEqual(found.parts.length, 1);
    assert.strictEqual(found.parts[0].kind, "place");
    assert.strictEqual(found.parts[0].label, "Place: Harbour Aquarium");
    assert.strictEqual(found.query, "fish place:\"harbour aquarium\" OR \"harbour aquarium\"");
    assert.deepStrictEqual(SearchCore.search(records(), found.query, {}).items, [1]);
    // (A named place of pictures without a town is a place too.)
    found = SearchCore.interpret(records(), "lions in city zoo", {});
    assert.strictEqual(found.parts[0].label, "Place: City Zoo");
});

test("a shop named only for a picture that shows one is found by what the viewer calls it",
     function () {
         var shops = records().concat([
             {i: 6, l: "lib", t: "A new car", g: ["car"], ds: 1, dk: "2021-03-08", gp: 1,
              np: "Example Motors", nk: "car", nd: 20},
             {i: 7, l: "lib", t: "Trying on a shirt", g: ["shirt"], ds: 1, dk: "2021-03-08",
              gp: 1, np: "Example Outfitters", nk: "clothes", nd: 8}]);
         function found(query) {
             return SearchCore.search(shops, query, {}).items.slice().sort();
         }
         ["kind:car", "kind:\"car dealer\"", "kind:\"car dealers\""].forEach(function (query) {
             assert.deepStrictEqual(found(query), [6], query);
         });
         assert.deepStrictEqual(found("kind:\"clothes shop\""), [7]);
         assert.strictEqual(VIEWER.format.namedPlaceNote(shops[5]), "car dealer, 20 m away");
         var said = SearchCore.interpret(shops, "car dealers", {});
         assert.deepStrictEqual(said.parts.map(function (part) {
             return part.label;
         }), ["Kind of place: car dealers"]);
         assert.deepStrictEqual(SearchCore.search(shops, said.query, {}).items, [6]);
     });

/* ---------- The viewer -------------------------------------------------------- */

test("what a named place is and how far it is", function () {
    var note = VIEWER.format.namedPlaceNote;
    assert.strictEqual(note(records()[0]), "aquarium, 60 m away");
    assert.strictEqual(note(records()[2]), "zoo, taken inside it");
    assert.strictEqual(note(records()[4]), "airport, taken inside it");
    assert.strictEqual(note({np: "Corner", nk: "place_of_worship", nd: 0}),
                       "place of worship, right there");
    assert.strictEqual(note({np: "Corner"}), "");
    // An approximate position (na): no distance, and probably inside.
    assert.strictEqual(note({np: "Harbour Aquarium", nk: "aquarium", na: 1}),
                       "aquarium, nearby");
    assert.strictEqual(note({np: "City Zoo", nk: "zoo", ni: 1, na: 1}),
                       "zoo, probably taken inside it");
    assert.strictEqual(note(records()[3]), "");
    assert.strictEqual(VIEWER.format.namedPlace(records()[3]), "");
    // A museum whose building holds the position, and a shop named for a picture that
    // shows one, by what it sells.
    assert.strictEqual(note({np: "Toy Museum", nk: "museum", ni: 1}),
                       "museum, taken inside it");
    assert.strictEqual(note({np: "Example Motors", nk: "car", nd: 20}),
                       "car dealer, 20 m away");
    assert.strictEqual(note({np: "Example Outfitters", nk: "clothes", nd: 8}),
                       "clothes shop, 8 m away");
});

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

// The window of a page: its document, address and fetch(), the rest made up.
function windowOf(document, location, fetch) {
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: false}, clipboard: null,
                    maxTouchPoints: 0},
        location: location,
        history: {state: null, scrollRestoration: "auto", pushState: function (state) {
            this.state = state;
        }, replaceState: function (state) {
            this.state = state;
        }, back: function () {}},
        localStorage: storage(),
        sessionStorage: storage(),
        innerWidth: 1000,
        innerHeight: 760,
        scrollY: 0,
        devicePixelRatio: 1,
        scrollTo: function () {},
        scrollBy: function () {},
        matchMedia: function () {
            return {matches: false, addEventListener: function () {}, addListener: function () {},
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
    context.addEventListener = function () {};
    context.removeEventListener = function () {};
    vm.createContext(context);
    return context;
}

function searchPage() {
    var document = FAKE.makeDocument();
    var config = {mode: "server", page: "search", api: "/api", tiles: "/tile",
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: {admin: false, static: false}};
    FAKE.parseBody(document, FAKE.PAGE, config);
    var context = windowOf(document, {search: "", hash: "", pathname: "/", protocol: "http:",
                                      href: "http://127.0.0.1:5000/",
                                      origin: "http://127.0.0.1:5000"}, function (url) {
        if (String(url).split("?")[0] === "/api/index") {
            return response(200, {version: 1, libraries: [{id: "lib", name: "Example"}],
                                  albums: [], records: records()});
        }
        return response(404, {error: "not here"});
    });
    FAKE.SCRIPTS.forEach(function (script) {
        vm.runInContext(script.code, context, {filename: script.name});
    });
    return {context: context, document: document};
}

// The Place fact of a record's information pane.
function placeOf(page, rec, query) {
    var box = page.document.createElement("div");
    page.document.body.appendChild(box);
    var chips = [];
    page.context.VIEWER.renderInfo(box, rec, null, {
        query: query || "",
        chipUrl: function (group, value) {
            return "/?" + group + "=" + encodeURIComponent(value);
        },
        onChip: function (group, value) {
            chips.push([group, value]);
        }
    });
    var terms = box.querySelectorAll("dt");
    for (var k = 0; k < terms.length; k++) {
        if (terms[k].textContent === "Place") {
            var value = terms[k].nextSibling;
            return {node: value, chips: chips};
        }
    }
    return null;
}

test("the viewer names the named place before the town", function () {
    var page = searchPage();
    return FAKE.waitFor(function () {
        return /\d+ pictures?/.test(FAKE.byId(page, "result-count").textContent);
    }).then(function () {
        var place = placeOf(page, records()[0]);
        var links = place.node.querySelectorAll("a");
        assert.strictEqual(links.length, 2);
        assert.strictEqual(links[0].textContent, "Harbour Aquarium");
        assert.strictEqual(links[0].href, "/?q=" +
                           encodeURIComponent("place:\"harbour aquarium\""));
        assert.ok(/Aquarium, 60 m away/.test(links[0].title), links[0].title);
        assert.strictEqual(links[1].textContent, "Coralport, North Coast, Examplia (2 km)");
        assert.ok(/\u00b7/.test(place.node.textContent), place.node.textContent);
        assert.ok(/aquarium, 60 m away$/.test(place.node.textContent), place.node.textContent);
        // A click searches for it in the page.
        links[0].dispatchEvent({type: "click", button: 0, preventDefault: function () {}});
        assert.deepStrictEqual(place.chips, [["q", "place:\"harbour aquarium\""]]);
        // Marked when the search is for it.
        place = placeOf(page, records()[0], "aquarium");
        assert.ok(place.node.querySelectorAll("a")[0].classList.contains("is-match"));
        // Without a town: the named place alone; without a named place: the town alone.
        place = placeOf(page, records()[2]);
        assert.strictEqual(place.node.querySelectorAll("a").length, 1);
        assert.ok(/^City Zoo/.test(place.node.textContent));
        assert.ok(!/\u00b7/.test(place.node.textContent));
        place = placeOf(page, records()[3]);
        assert.strictEqual(place.node.textContent, "Coralport, North Coast, Examplia (2 km)");
    });
});

test("the viewer marks a named place found by its kind", function () {
    var page = searchPage();
    var falls = {i: 6, l: "lib", t: "Mist over the rocks", g: ["mist"], ds: 1,
                 dk: "2021-03-08", gp: 1, np: "High Falls", nk: "waterfall", nd: 30};
    function marked(rec, query) {
        var place = placeOf(page, rec, query);
        var marks = place.node.querySelectorAll("mark");
        return [place.node.querySelectorAll("a")[0].classList.contains("is-match"),
                marks.length ? marks[0].textContent : "", place.node.textContent];
    }
    return FAKE.waitFor(function () {
        return /\d+ pictures?/.test(FAKE.byId(page, "result-count").textContent);
    }).then(function () {
        // "High Falls" never says waterfall: the chip and the kind under it are marked.
        assert.deepStrictEqual(marked(falls, "kind:waterfall OR waterfalls"),
                               [true, "waterfall", "High Fallswaterfall, 30 m away"]);
        assert.deepStrictEqual(marked(falls, SearchCore.parse("kind:waterfalls")).slice(0, 2),
                               [true, "waterfall"]);
        // Another kind, a left-out kind, or only words: not marked.
        ["kind:zoo", "mist -kind:waterfall", "mist"].forEach(function (query) {
            assert.deepStrictEqual(marked(falls, query).slice(0, 2), [false, ""], query);
        });
        // The maps' kind in words: an aerodrome is an airport.
        assert.deepStrictEqual(marked(records()[4], "kind:airport OR airports").slice(0, 2),
                               [true, "airport"]);
    });
});

/* ---------- The map ----------------------------------------------------------- */

var MAP_PAGE = fs.readFileSync(path.join(ROOT, "templates", "map.html"), "utf8");
var MAP_SCRIPTS = ["site.js", "shell.js", "search-core.js", "streets.js", "routes.js",
                   "viewer.js", "map.js"].map(function (name) {
    return {name: name, code: fs.readFileSync(path.join(STATIC, name), "utf8")};
});
var POINTS = [{id: 1, lat: 5.0, lon: -150.0}, {id: 2, lat: 5.0, lon: -150.0},
              {id: 4, lat: 5.0, lon: -150.0}, {id: 3, lat: -20.0, lon: 30.0}];

function mapPage(searchText) {
    var document = FAKE.makeDocument();
    var config = {mode: "server", page: "map", api: "/api", tiles: "/tile", tilesMax: 8,
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: {admin: false, static: false}};
    FAKE.parseBody(document, MAP_PAGE, config);
    var canvas = document.getElementById("map-canvas");
    canvas.getContext = function () {
        return new Proxy({}, {get: function (target, name) {
            if (name === "measureText") {
                return function (text) {
                    return {width: String(text).length * 6};
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
    var context = windowOf(document, {search: searchText, hash: "", pathname: "/map",
                                      protocol: "http:",
                                      href: "http://127.0.0.1:5000/map" + searchText,
                                      origin: "http://127.0.0.1:5000"}, function (url) {
        var plain = String(url).split("?")[0];
        if (plain === "/api/index") {
            return response(200, {version: 1, libraries: [{id: "lib", name: "Example"}],
                                  albums: [], records: records()});
        }
        if (plain === "/api/locations") {
            return response(200, POINTS);
        }
        if (plain === "/api/routes") {
            return response(200, {routes: []});
        }
        return response(404, {error: "not here"});
    });
    MAP_SCRIPTS.forEach(function (script) {
        vm.runInContext(script.code, context, {filename: script.name});
    });
    return {document: document};
}

test("the map's list of a pin names the named places", function () {
    var page = mapPage("?view=5,-150,12&pin=5,-150");
    var title = page.document.getElementById("map-panel-title");
    var list = page.document.getElementById("map-panel-list");
    return FAKE.waitFor(function () {
        return /pictures/.test(title.textContent) && list.childNodes.length === 3;
    }).then(function () {
        // (Two of the three pictures were taken at the aquarium.)
        assert.strictEqual(title.textContent, "3 pictures \u00b7 Harbour Aquarium");
        var places = list.querySelectorAll(".map-panel-place").map(function (node) {
                return node.textContent;
            });
        assert.deepStrictEqual(places, ["Harbour Aquarium", "Harbour Aquarium"]);
    });
});

var failed = 0;
tests.reduce(function (chain, entry) {
    return chain.then(function () {
        return Promise.resolve().then(entry.body).then(function () {
            console.log("ok   " + entry.name);
        }, function (error) {
            failed += 1;
            console.log("FAIL " + entry.name + "\n     " + (error && error.stack || error));
        });
    });
}, Promise.resolve()).then(function () {
    console.log((tests.length - failed) + " of " + tests.length + " passed");
    process.exit(failed ? 1 : 0);
});
