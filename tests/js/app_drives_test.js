/* Tests for drives on the server's search page (static/app.js with
   static/search-core.js and static/viewer.js), as the admin sees them: the
   real scripts on the made-up DOM of tests/js/app_meaning_test.js, with a
   fake fetch() for /api/index. A drive (dv: video clips recorded one after
   the other) folds into one tile; its clips are those the search may show.
   The records are all made up here.

   Run by tests/test_drives.py, or directly:
       node tests/js/app_drives_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var path = require("path");
var vm = require("vm");

var FAKE = require(path.join(__dirname, "app_meaning_test.js"));
var waitFor = FAKE.waitFor;
var byId = FAKE.byId;
var search = FAKE.search;
var notes = FAKE.notes;

/* A drive of six one-minute clips (the second hidden, the fourth graphic,
   the drive's own words on the first) and a photo. */
function records() {
    var titles = ["Leaving the garage", "Hidden clip", "Suburban street", "Crash scene",
                  "Gas station at a crossing", "Bridge over a river"];
    var list = titles.map(function (title, k) {
        var rec = {i: 31 + k, l: "lib", t: title, ds: 1, mv: 1, dr: 60, vc: "h264",
                   ty: "video", dk: "2023-08-17", ts: "2023-08-17T14:0" + k + ":00", dv: 31};
        if (k) {
            rec.dx = k;
        }
        return rec;
    });
    list[0].dvt = "Morning drive to the coast";
    list[0].dvd = "Along a coastal highway past a lighthouse.";
    list[1].h = 1;
    list[2].fav = 1;
    list[3].se = "graphic";
    list[4].fav = 1;
    list.push({i: 50, l: "lib", t: "A lighthouse at dusk", ds: 1, dk: "2023-08-16"});
    return list;
}

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

/* The admin's search page on the server (searchText: its address's ?...
   part); page.opened: the options of the viewer's last VIEWER.open (the
   viewer itself is not opened); page.sent: the marks sent. */
function serverPage(searchText) {
    var data = records();
    var sent = [];
    var document = FAKE.makeDocument();
    document.createElementNS = function (ns, tag) {
        var node = document.createElement(tag);
        node.namespaceURI = ns;
        return node;
    };
    var config = {mode: "server", page: "search", api: "/api", tiles: "/tile",
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: {admin: true, static: false}};
    FAKE.parseBody(document, FAKE.PAGE, config);
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
    function fetch(url, init) {
        var plain = String(url).split("?")[0];
        if (plain === "/api/index") {
            return response(200, {version: 1, libraries: [{id: "lib", name: "Example"}],
                                  records: data});
        }
        if (plain === "/api/admin/marks") {
            var body = JSON.parse(init.body);
            sent.push(["/marks", body]);
            var marks = {};
            body.ids.forEach(function (id) {
                marks[id] = {favorite: Boolean(body.favorite), rating: 0};
            });
            return response(200, {changed: body.ids.length, marks: marks});
        }
        return response(404, {error: "not here"});
    }
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: false}, clipboard: null},
        location: {search: searchText || "", hash: "", pathname: "/", protocol: "http:",
                   href: "http://127.0.0.1:5000/" + (searchText || ""),
                   origin: "http://127.0.0.1:5000"},
        history: {state: null, scrollRestoration: "auto", pushState: function (state) {
            this.state = state;
        }, replaceState: function (state) {
            this.state = state;
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
        atob: function (text) {
            return Buffer.from(text, "base64").toString("binary");
        },
        btoa: function (text) {
            return Buffer.from(text, "binary").toString("base64");
        },
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
    var page = {context: context, document: document, opened: null, sent: sent};
    context.VIEWER.open = function (options) {
        page.opened = options;
        return true;
    };
    return page;
}

// A value of the page's own realm as plain data (deepStrictEqual compares
// prototypes too).
function plain(value) {
    return JSON.parse(JSON.stringify(value));
}

function count(page) {
    return byId(page, "result-count").textContent;
}

function tiles(page) {
    return byId(page, "grid").querySelectorAll(".tile");
}

// The titles of the tiles, in order.
function tileTitles(page) {
    return tiles(page).map(function (tile) {
        var title = tile.querySelector(".tile-title");
        return title ? title.textContent : "";
    });
}

function loaded(page) {
    return waitFor(function () {
        return /\d+ (pictures?|clips?)/.test(count(page)) && tiles(page).length > 0;
    });
}

// Clicks the link of the tile at index, as a visitor does (opens the viewer).
function openTile(page, index) {
    var link = tiles(page)[index].querySelector(".tile-link");
    link.dispatchEvent({type: "click", button: 0});
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

/* ---------- Tests ------------------------------------------------------------------- */

test("a folded drive holds the clips the search shows, not hidden or sensitive ones", function () {
    var page = serverPage();
    return loaded(page).then(function () {
        // Two tiles: the drive (its own title, its four clips that show) and
        // the photo.
        assert.strictEqual(tiles(page).length, 2);
        var drive = tiles(page)[0];
        assert.ok(drive.classList.contains("is-drive"));
        assert.strictEqual(tileTitles(page)[0], "Morning drive to the coast");
        assert.ok(/Drive 4:00/.test(drive.textContent), drive.textContent);
        var button = drive.querySelector(".tile-drive");
        assert.strictEqual(button.textContent, "\u00d74");
        assert.ok(/4 clips in 1 drive/.test(notes(page)), notes(page));
        // The viewer plays those four, from the clip the tile shows (the
        // middle one when nothing was searched: not the parked start).
        openTile(page, 0);
        var opened = page.opened;
        assert.ok(opened, "the viewer was asked to open");
        var shownId = opened.ids[opened.index];
        assert.strictEqual(shownId, 33);
        var about = opened.driveOf(shownId);
        assert.deepStrictEqual(plain(about.clips), [31, 33, 35, 36]);
        assert.strictEqual(about.text, 31);
        assert.strictEqual(about.folded, true);
        // Opened in the grid: those four clips, in order.
        button.click();
        return waitFor(function () {
            return tiles(page).length === 5;
        });
    }).then(function () {
        assert.deepStrictEqual(plain(tileTitles(page).slice(0, 4)),
                               ["Leaving the garage", "Suburban street",
                                "Gas station at a crossing", "Bridge over a river"]);
    });
});

test("a search hit inside a drive shows the drive as that clip, ringed when opened", function () {
    var page = serverPage();
    return loaded(page).then(function () {
        search(page, "gas station");
        return waitFor(function () {
            return tiles(page).length === 1;
        });
    }).then(function () {
        // The clip of the gas station stands for the drive (not the first
        // clip, which carries the drive's own words).
        openTile(page, 0);
        var opened = page.opened;
        assert.strictEqual(opened.ids[opened.index], 35);
        var about = opened.driveOf(35);
        assert.deepStrictEqual(plain(about.matched), {35: true});
        tiles(page)[0].querySelector(".tile-drive").click();
        return waitFor(function () {
            return tiles(page).length === 4;
        });
    }).then(function () {
        var ringed = tiles(page).filter(function (tile) {
            return tile.classList.contains("is-match");
        });
        assert.strictEqual(ringed.length, 1);
        assert.strictEqual(ringed[0].querySelector(".tile-title").textContent,
                           "Gas station at a crossing");
    });
});

test("with drives not folded a clip still plays its drive in the viewer", function () {
    var page = serverPage();
    return loaded(page).then(function () {
        var box = byId(page, "drives");
        assert.strictEqual(byId(page, "drives-tool").hidden, false);
        box.checked = false;
        box.dispatchEvent({type: "change"});
        return waitFor(function () {
            return tiles(page).length === 5;
        });
    }).then(function () {
        // The drive's words still find it: as its middle clip, which plays
        // the drive (with its own title and description in the viewer).
        search(page, "lighthouse");
        return waitFor(function () {
            return tiles(page).length === 2;
        });
    }).then(function () {
        var titles = tileTitles(page);
        var at = titles.indexOf("Suburban street");
        assert.ok(at >= 0, titles.join(", "));
        openTile(page, at);
        var opened = page.opened;
        var about = opened.driveOf(opened.ids[opened.index]);
        assert.deepStrictEqual(plain(about.clips), [31, 33, 35, 36]);
        assert.strictEqual(about.folded, false);
        assert.strictEqual(about.text, 31);
    });
});

test("selecting a drive's tile selects the clips the search found, in the drive's order",
     function () {
    var page = serverPage();
    function bar() {
        return byId(page, "selection-bar");
    }
    function button(pattern) {
        return bar().querySelectorAll("button").filter(function (node) {
            return pattern.test(node.textContent);
        })[0];
    }
    return loaded(page).then(function () {
        // Two clips of the drive are favorites: one tile, the drive.
        search(page, "is:favorite");
        return waitFor(function () {
            return tiles(page).length === 1;
        });
    }).then(function () {
        assert.ok(tiles(page)[0].classList.contains("is-drive"));
        byId(page, "select-button").click();
        assert.strictEqual(button(/^Select all/).textContent, "Select all 2 results");
        button(/^Select all/).click();
        assert.ok(/2 selected/.test(bar().textContent), bar().textContent);
        // All of them are favorites: Favorite takes the star away from those
        // two only, in the drive's order (never from the drive's other clips).
        button(/Favorite/).click();
        return waitFor(function () {
            return page.sent.length === 1;
        });
    }).then(function () {
        assert.deepStrictEqual(plain(page.sent[0]), ["/marks", {ids: [33, 35], favorite: false}]);
    });
});

test("the selected pictures go to the server in the order the results show them", function () {
    // Oldest first: the photo, then the drive's clips in its order.
    var page = serverPage("?sort=oldest");
    var bar = function () {
        return byId(page, "selection-bar");
    };
    function button(pattern) {
        return bar().querySelectorAll("button").filter(function (node) {
            return pattern.test(node.textContent);
        })[0];
    }
    return loaded(page).then(function () {
        byId(page, "select-button").click();
        button(/^Select all/).click();
        button(/Favorite/).click();
        return waitFor(function () {
            return page.sent.length === 1;
        });
    }).then(function () {
        assert.deepStrictEqual(plain(page.sent[0]),
                               ["/marks", {ids: [50, 31, 33, 35, 36], favorite: true}]);
    });
});

/* ---------- Run ---------------------------------------------------------------------- */

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
