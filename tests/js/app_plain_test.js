/* Tests for plain-language search on the server's search page as the admin
   sees it (static/app.js, site.js, shell.js, search-core.js): the real
   scripts on the made-up DOM of tests/js/app_meaning_test.js, with a fake
   fetch() for the server's answers (/api/index, /api/people, /api/vectors,
   /api/embed and the admin's album routes). The chips over the results
   (#plain-chips): what was understood, the x that leaves a part as typed
   (kept in the address), "Search the words as typed", the question about
   a word that may be a person or a place, the words searched by their
   words only (a name); one /api/embed request for the query and its words;
   Save as album saving the search as understood; the switch that turns it
   all off. The pictures and people are all made up here.

   Run by tests/test_plain_language.py, or directly:
       node tests/js/app_plain_test.js
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

var DIMS = 8;
var MODEL = "m-plain";

function unit(values) {
    var norm = Math.sqrt(values.reduce(function (sum, value) {
        return sum + value * value;
    }, 0));
    return values.map(function (value) {
        return Math.round(value / norm * 127);
    });
}

// A vector whose cosine with axis 0 is cos (the rest on axis).
function near(cos, axis) {
    var values = [0, 0, 0, 0, 0, 0, 0, 0];
    values[0] = cos;
    values[axis] = Math.sqrt(1 - cos * cos);
    return unit(values);
}

function base64(numbers) {
    return Buffer.from(Int8Array.from(numbers).buffer).toString("base64");
}

function records() {
    var list = [
        {i: 1, l: "lib", t: "Anna at the beach", g: ["beach"], dk: "2024-11-03", ds: 1,
         ci: "Lisbon", rg: "Lisbon District", co: "Portugal", cc: "PT"},
        {i: 2, l: "lib", t: "A tram", g: ["tram"], dk: "2019-06-20", ds: 1, ci: "Lisbon",
         rg: "Lisbon District", co: "Portugal", cc: "PT"},
        {i: 3, l: "lib", t: "Beach umbrellas", g: ["beach"], dk: "2024-11-05", ds: 1,
         ci: "Lisbon", rg: "Lisbon District", co: "Portugal", cc: "PT", f: "Anna"},
        {i: 4, l: "lib", t: "Old bridge", g: ["bridge"], dk: "2016-04-10", ds: 1,
         ci: "Siena", rg: "Tuscany", co: "Italy", cc: "IT"},
        {i: 5, l: "lib", t: "Dinner", g: ["dinner"], dk: "2025-07-14", ds: 1},
        {i: 6, l: "lib", t: "Sea turtle", g: ["turtle"], dk: "2023-05-01", ds: 1,
         f: "Kowalski"},
        {i: 7, l: "lib", t: "Turtle painted on a wall", g: ["turtle", "wall"], dk: "2023-05-02", ds: 1}
    ];
    // (Sixty more: a library, not a small set, where per-term coverage applies
    // -- see search-core.js MEANING_SMALL.)
    for (var k = 0; k < 60; k++) {
        list.push({i: 100 + k, l: "lib", t: "Filler " + k, g: ["filler"], dk: "2010-01-01",
                   ds: 1});
    }
    return list;
}

function vectorOf(rec) {
    return rec.i === 6 ? near(0.9, 1) : rec.i === 7 ? near(0.88, 2)
        : near(0.05 + 0.4 * ((rec.i * 7) % 40) / 40, 6 + (rec.i % 2));
}

var PEOPLE = [{id: 1, name: "Anna Lopez", label: "Anna Lopez", cover: null, faces: 2, ids: [1, 2]},
              {id: 2, name: "Siena", label: "Siena", cover: null, faces: 1, ids: [5]}];

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

/* The admin's search page; searchText: its address's ?... part; options:
   plain (false: config.json turned plain-language search off), meaning
   (true: the server has meaning vectors and embeds queries), people (false:
   People is not shown to the requester: a visitor, unless admin is true:
   the admin with People off).
   page.log: requests (urls), sent ([admin route, body]), embeds (the q
   values of each /api/embed request). */
function serverPage(searchText, options) {
    options = options || {};
    var log = {requests: [], sent: [], embeds: [], urls: []};
    var data = {records: records()};
    var document = FAKE.makeDocument();
    document.createElementNS = function (ns, tag) {
        var node = document.createElement(tag);
        node.namespaceURI = ns;
        return node;
    };
    var capabilities = {admin: options.admin !== undefined ? options.admin
                        : options.people !== false, static: false,
                        people: options.people !== false};
    if (options.plain === false) {
        capabilities.plain = false;
    }
    var config = {mode: "server", page: "search", api: "/api", tiles: "/tile",
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}], capabilities: capabilities};
    FAKE.parseBody(document, FAKE.PAGE, config);
    function fetch(url, init) {
        log.requests.push(url);
        var plain = String(url).split("?")[0];
        var body = init && init.body ? JSON.parse(init.body) : null;
        if (plain === "/api/index") {
            return response(200, {version: 1, libraries: [{id: "lib", name: "Example"}],
                                  albums: [], records: data.records});
        }
        if (plain === "/api/people") {
            return response(200, {version: 1, people: PEOPLE});
        }
        if (plain === "/api/vectors") {
            if (!options.meaning) {
                return response(200, {model: "", dims: 0, query: false, count: 0, ids: [],
                                      data: ""});
            }
            return response(200, {model: MODEL, dims: DIMS, query: true,
                                  count: data.records.length,
                                  ids: data.records.map(function (rec) {
                                      return rec.i;
                                  }),
                                  data: base64([].concat.apply([], data.records.map(vectorOf)))});
        }
        if (plain === "/api/embed") {
            var texts = new URLSearchParams(String(url).split("?")[1]).getAll("q");
            log.embeds.push(texts);
            // (Every text means turtles here: axis 0. A server that gives
            // fewer vectors than asked for: options.embedLimit.)
            var vecs = texts.slice(0, options.embedLimit || texts.length).map(function () {
                return base64(near(1, 1));
            });
            var answer = {model: MODEL, dims: DIMS, vec: vecs[0]};
            if (texts.length > 1) {
                answer.vecs = vecs;
            }
            return response(200, answer);
        }
        var admin = /^\/api\/admin(\/.*)$/.exec(plain);
        if (admin) {
            log.sent.push([admin[1], body]);
            if (admin[1] === "/albums" && body) {
                return response(201, {album: {id: 50, name: body.name, kind: "smart",
                                              cover: null, query: body.query,
                                              options: body.options || {}}});
            }
        }
        return response(404, {error: "not here"});
    }
    var context = pageContext(document, log, {
        location: {search: searchText || "", hash: "", pathname: "/", protocol: "http:",
                   href: "http://127.0.0.1:5000/" + (searchText || ""),
                   origin: "http://127.0.0.1:5000"},
        fetch: fetch
    });
    vm.createContext(context);
    FAKE.SCRIPTS.forEach(function (script) {
        vm.runInContext(script.code, context, {filename: script.name});
    });
    return {context: context, document: document, log: log, data: data};
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

/* The window of a page (its scripts' global object): document, log (urls:
   the addresses written to the history; error: the last console error) and
   extra (location, fetch, SEARCH_DATA_MANIFEST, ...). */
function pageContext(document, log, extra) {
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: false}, clipboard: null},
        history: {state: null, scrollRestoration: "auto", pushState: function (state, title,
                                                                             url) {
            this.state = state;
            log.urls.push(url);
        }, replaceState: function (state, title, url) {
            this.state = state;
            log.urls.push(url);
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
        console: {log: function () {}, warn: function () {}, error: function (text) {
            log.error = String(text);
        }},
        atob: function (text) {
            return Buffer.from(text, "base64").toString("binary");
        },
        btoa: function (text) {
            return Buffer.from(text, "binary").toString("base64");
        },
        URLSearchParams: URLSearchParams,
        URL: URL
    };
    Object.keys(extra).forEach(function (key) {
        context[key] = extra[key];
    });
    context.window = context;
    context.addEventListener = function () {};
    context.removeEventListener = function () {};
    return context;
}

/* A published site's search page (export.py: no server, no People): its
   records (options.records, default records()), the names it publishes
   (options.names: manifest.names, its "people_names" option, which are
   tags of their pictures there) and capabilities besides admin and static
   (options.capabilities; default: the places of the pictures). */
function exportPage(searchText, options) {
    options = options || {};
    var log = {requests: [], urls: []};
    var data = {records: options.records || records()};
    var document = FAKE.makeDocument();
    var capabilities = {admin: false, static: true};
    var extra = options.capabilities || {places: true};
    Object.keys(extra).forEach(function (key) {
        capabilities[key] = extra[key];
    });
    var config = {mode: "static", page: "search", data: "data/", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}], capabilities: capabilities,
                  title: "Example"};
    FAKE.parseBody(document, FAKE.PAGE, config);
    var manifest = {format: 1, version: 3, count: data.records.length,
                    shards: ["index-000.js"], details: [], library: {id: "lib", name: "Example"},
                    libraries: [{id: "lib", name: "Example"}], capabilities: capabilities};
    if (options.names) {
        manifest.names = options.names;
    }
    var context = pageContext(document, log, {
        location: {search: searchText || "", hash: "", pathname: "/x/index.html",
                   protocol: "file:", href: "file:///x/index.html" + (searchText || ""),
                   origin: "null"},
        SEARCH_DATA_MANIFEST: manifest
    });
    // The site's scripts run (as theirs would) a moment after they are added.
    document.onAppend = function (node) {
        if (node.tagName !== "SCRIPT") {
            return;
        }
        log.requests.push(String(node.src || "").split("?")[0]);
        setTimeout(function () {
            if (/index-000\.js/.test(String(node.src || ""))) {
                context.SEARCH_DATA.push(data.records);
            }
            if (node.onload) {
                node.onload();
            }
        }, 5);
    };
    vm.createContext(context);
    FAKE.SCRIPTS.forEach(function (script) {
        vm.runInContext(script.code, context, {filename: script.name});
    });
    return {context: context, document: document, log: log, data: data};
}

// The examples of the search tips (#syntax-help), opened.
function tipExamples(page) {
    byId(page, "syntax-button").click();
    return byId(page, "syntax-help").querySelectorAll(".syntax-example").map(function (node) {
        return node.textContent;
    });
}

// The texts of the chips row (#plain-chips), one per item.
function chips(page) {
    var box = byId(page, "plain-chips");
    return box.hidden ? [] : box.children.map(function (node) {
        return node.textContent.replace(/\u00d7$/, "").replace(/\s+/g, " ").trim();
    });
}

function chip(page, text) {
    return byId(page, "plain-chips").children.filter(function (node) {
        return node.textContent.indexOf(text) === 0;
    })[0];
}

function buttonNamed(node, text) {
    return node.querySelectorAll("button").filter(function (button) {
        return button.textContent === text;
    })[0];
}

function count(page) {
    return byId(page, "result-count").textContent;
}

function lastUrl(page) {
    return page.log.urls[page.log.urls.length - 1] || "";
}

function loaded(page) {
    return waitFor(function () {
        return byId(page, "result-count").textContent !== "";
    });
}

// The page has its pictures (not "Loading the pictures...").
function shown(page) {
    return waitFor(function () {
        return /\d+ pictures?/.test(byId(page, "result-count").textContent);
    });
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

test("the chips say what was understood; x leaves a part as typed", function () {
    var page = serverPage("?q=anna+at+the+beach+in+lisbon");
    return loaded(page).then(function () {
        return waitFor(function () {
            return chips(page).length > 2;
        });
    }).then(function () {
        assert.deepStrictEqual(chips(page), ["Searching for", "Person: Anna Lopez",
                                             "Place: Lisbon", "\u201cat the beach\u201d by words",
                                             "Search the words as typed"]);
        // Her picture at the beach in Lisbon, and the beach in her folder there.
        assert.strictEqual(count(page), "2 pictures");
        // The search box keeps what was typed.
        assert.strictEqual(byId(page, "query").value, "anna at the beach in lisbon");
        // People were asked for at once (a plain word may be a name).
        assert.ok(page.log.requests.indexOf("/api/people?hidden=1") >= 0,
                  page.log.requests.join(" "));
        chip(page, "Person: Anna Lopez").click();
        return waitFor(function () {
            return chips(page).indexOf("As typed: anna") >= 0;
        });
    }).then(function () {
        assert.ok(/[?&]typed=anna(&|$)/.test(lastUrl(page)), lastUrl(page));
        assert.deepStrictEqual(chips(page).slice(0, 2), ["Searching for", "Place: Lisbon"]);
        // The word as typed: the picture with "Anna" in its title, and her folder.
        assert.strictEqual(count(page), "2 pictures");
        // Understood again.
        chip(page, "As typed: anna").click();
        return waitFor(function () {
            return chips(page).indexOf("Person: Anna Lopez") >= 0;
        });
    }).then(function () {
        assert.ok(!/typed=/.test(lastUrl(page)), lastUrl(page));
        buttonNamed(byId(page, "plain-chips"), "Search the words as typed").click();
        return waitFor(function () {
            return chips(page).indexOf("Words as typed") >= 0;
        });
    }).then(function () {
        assert.deepStrictEqual(chips(page), ["Searching for", "Words as typed"]);
        assert.ok(/[?&]typed=\*(&|$)/.test(lastUrl(page)), lastUrl(page));
        // (The same two: "lisbon" is a word of their places too.)
        assert.strictEqual(count(page), "2 pictures");
        chip(page, "Words as typed").click();
        return waitFor(function () {
            return chips(page).indexOf("Place: Lisbon") >= 0;
        });
    });
});

test("a word that may be a person or a place is asked about", function () {
    var page = serverPage("?q=siena");
    return loaded(page).then(function () {
        return waitFor(function () {
            return byId(page, "plain-chips").querySelector(".plain-choice");
        });
    }).then(function (choice) {
        assert.deepStrictEqual(choice.querySelectorAll("button").map(function (button) {
            return button.textContent;
        }), ["Person: Siena", "Place: Siena", "the word \u201csiena\u201d"]);
        buttonNamed(choice, "Place: Siena").click();
        return waitFor(function () {
            return byId(page, "query").value !== "siena";
        });
    }).then(function () {
        // The choice is written into the box: it stays chosen.
        assert.strictEqual(byId(page, "query").value, "place:siena OR siena");
        assert.strictEqual(count(page), "1 picture");
        assert.strictEqual(byId(page, "plain-chips").hidden, true);
    });
});

test("Save as album keeps the search as understood", function () {
    var page = serverPage("?q=anna+beach+2024");
    return loaded(page).then(function () {
        return waitFor(function () {
            return chips(page).indexOf("Taken: 2024") >= 0 &&
                chips(page).indexOf("Person: Anna Lopez") >= 0;
        });
    }).then(function () {
        byId(page, "save-search").click();
        var dialog = page.document.querySelector(".modal");
        var hint = dialog.querySelector(".field-hint").textContent;
        assert.ok(hint.indexOf("\u201cperson:\"Anna Lopez\" OR anna beach date:2024 OR 2024\u201d") >=
                  0, hint);
        // (Where People is not shown, the words typed: albums.alternative_spans.)
        assert.ok(hint.indexOf("Visitors who cannot see People get it with the words typed " +
                               "for the person") >= 0, hint);
        buttonNamed(dialog, "Save").click();
        return waitFor(function () {
            return page.log.sent.length;
        });
    }).then(function () {
        assert.deepStrictEqual(page.log.sent[0], ["/albums", {
            query: "person:\"Anna Lopez\" OR anna beach date:2024 OR 2024", options: {},
            name: "anna beach 2024", kind: "smart"}]);
    });
});

test("by words and meaning: one request for the query and its words; names by their words",
     function () {
         var page = serverPage("?q=kowalski+turtle", {meaning: true});
         return loaded(page).then(function () {
             return waitFor(function () {
                 return chips(page).length && page.log.embeds.length;
             });
         }).then(function () {
             assert.deepStrictEqual(page.log.embeds, [["kowalski turtle", "kowalski",
                                                       "turtle"]]);
             return waitFor(function () {
                 return chips(page).indexOf("\u201ckowalski\u201d by its words only") >= 0;
             });
         }).then(function () {
             // Kowalski is only a folder name: the turtle in that folder, not the
             // painted one (nothing of Kowalski there), however much it means turtles.
             assert.strictEqual(count(page), "1 picture");
             assert.deepStrictEqual(chips(page), [
                 "Searching for", "\u201ckowalski turtle\u201d by words and meaning",
                 "\u201ckowalski\u201d by its words only"]);
             // The vectors are kept for the tab: a reload asks for nothing.
             var kept = JSON.parse(page.context.sessionStorage.getItem(
                 "searchImages.queryVectors"));
             assert.deepStrictEqual(Object.keys(kept).sort(), [MODEL + "\nkowalski",
                                                               MODEL + "\nkowalski turtle",
                                                               MODEL + "\nturtle"]);
         });
     });

test("Search the words as typed is for that search only", function () {
    var page = serverPage("?q=anna+at+the+beach+in+lisbon");
    return loaded(page).then(function () {
        return waitFor(function () {
            return chips(page).indexOf("Person: Anna Lopez") >= 0;
        });
    }).then(function () {
        buttonNamed(byId(page, "plain-chips"), "Search the words as typed").click();
        return waitFor(function () {
            return chips(page).indexOf("Words as typed") >= 0;
        });
    }).then(function () {
        // A new search is understood again (and typed= leaves the address).
        search(page, "anna in siena 2016");
        return waitFor(function () {
            return chips(page).indexOf("Place: Siena") >= 0;
        });
    }).then(function () {
        assert.deepStrictEqual(chips(page).slice(0, 4), ["Searching for", "Person: Anna Lopez",
                                                        "Place: Siena", "Taken: 2016"]);
        assert.ok(!/typed=/.test(lastUrl(page)), lastUrl(page));
    });
});

test("an understood part that leaves nothing says so, with a way out", function () {
    // (No picture is from last summer: the newest is from July 2025.)
    var page = serverPage("?q=beach+last+summer");
    return loaded(page).then(function () {
        return waitFor(function () {
            return !byId(page, "empty").hidden && byId(page, "empty").textContent;
        });
    }).then(function () {
        var text = byId(page, "empty").textContent;
        assert.ok(/No picture was taken then \(June . August \d{4}\); the newest is from July 2025\./
                  .test(text), text);
        assert.ok(text.indexOf("2 pictures match without Taken: June") >= 0, text);
        assert.ok(text.indexOf("Check the spelling") < 0, text);
        buttonNamed(byId(page, "empty"), "Leave out \u201clast summer\u201d").click();
        return waitFor(function () {
            return byId(page, "query").value === "beach";
        });
    }).then(function () {
        assert.strictEqual(count(page), "2 pictures");
        // A person whose pictures are from another year: without that year.
        search(page, "anna tram 2024");
        return waitFor(function () {
            return !byId(page, "empty").hidden &&
                byId(page, "empty").textContent.indexOf("without Taken: 2024") >= 0;
        });
    }).then(function () {
        var text = byId(page, "empty").textContent;
        assert.ok(text.indexOf("1 picture matches without Taken: 2024 (\u201c2024\u201d).") >= 0,
                  text);
    });
});

test("the chips row for screen readers: names, a live region, the focus kept", function () {
    var page = serverPage("?q=anna+at+the+beach+in+lisbon");
    return loaded(page).then(function () {
        return waitFor(function () {
            return chips(page).indexOf("Person: Anna Lopez") >= 0;
        });
    }).then(function () {
        var box = byId(page, "plain-chips");
        assert.strictEqual(box.getAttribute("role"), "group");
        assert.strictEqual(box.getAttribute("aria-live"), "polite");
        var person = chip(page, "Person: Anna Lopez");
        assert.strictEqual(person.getAttribute("aria-label"),
                           "Person: Anna Lopez \u2013 search \u201canna\u201d as typed words " +
                           "instead");
        person.click();
        return waitFor(function () {
            return chips(page).indexOf("As typed: anna") >= 0;
        });
    }).then(function () {
        // (The focus is on the button now where the one clicked was.)
        var active = page.document.activeElement;
        assert.ok(byId(page, "plain-chips").contains(active), String(active && active.tagName));
        assert.strictEqual(active.textContent.replace(/\u00d7$/, ""), "Place: Lisbon");
        var siena = serverPage("?q=siena");
        return loaded(siena).then(function () {
            return waitFor(function () {
                return byId(siena, "plain-chips").querySelector(".plain-choice");
            });
        }).then(function (choice) {
            assert.strictEqual(choice.getAttribute("aria-label"),
                               "\u201csiena\u201d: person, place or words?");
        });
    });
});

test("the search tips say what is understood here", function () {
    var page = serverPage("");
    return loaded(page).then(function () {
        byId(page, "syntax-button").click();
        var tips = byId(page, "syntax-help").textContent;
        assert.ok(tips.indexOf("plain language: the names of people and places, years, months " +
                               "and seasons become filters") >= 0, tips);
        var visitor = serverPage("", {people: false});
        return loaded(visitor).then(function () {
            byId(visitor, "syntax-button").click();
            var text = byId(visitor, "syntax-help").textContent;
            assert.ok(text.indexOf("plain language: the names of places, years, months and " +
                                   "seasons become filters") >= 0, text);
            assert.ok(text.indexOf("beach last summer") >= 0, text);
        });
    });
});

test("the search tips leave out person: where it finds nobody", function () {
    // (Known as K1: a person:"Anna" row that gave "0 pictures".)
    var admin = serverPage("");
    var visitor = serverPage("", {people: false});
    var peopleOff = serverPage("", {people: false, admin: true});
    var published = exportPage("", {names: ["Anna Lopez"]});
    var unnamed = exportPage("");
    return Promise.all([admin, visitor, peopleOff, published, unnamed].map(shown)).then(
        function () {
            // The admin with People: person: is there.
            assert.ok(tipExamples(admin).indexOf("person:\"Anna\"") >= 0);
            // A visitor People is not shown to, or the admin with People off: not.
            [visitor, peopleOff].forEach(function (page) {
                var examples = tipExamples(page);
                assert.ok(examples.indexOf("person:\"Anna\"") < 0, examples.join(" | "));
                assert.ok(examples.indexOf("beach last summer") >= 0, examples.join(" | "));
                assert.ok(examples.indexOf("is:360") >= 0, examples.join(" | "));
            });
            // A published site never has it (site.js loads no people there),
            // with or without the names it publishes; plain language stays,
            // with what it understands there.
            var examples = tipExamples(published);
            assert.ok(examples.indexOf("person:\"Anna\"") < 0, examples.join(" | "));
            assert.ok(examples.indexOf("anna in lisbon last summer") >= 0, examples.join(" | "));
            assert.ok(byId(published, "syntax-help").textContent.indexOf(
                "plain language: the names of people and places, years, months and seasons") >= 0);
            examples = tipExamples(unnamed);
            assert.ok(examples.indexOf("person:\"Anna\"") < 0, examples.join(" | "));
            assert.ok(examples.indexOf("beach last summer") >= 0, examples.join(" | "));
            assert.ok(byId(unnamed, "syntax-help").textContent.indexOf(
                "plain language: the names of places, years, months and seasons") >= 0);
            // (The rows of the count of people stay: people:none is no person:.)
            assert.ok(examples.indexOf("people:none") >= 0, examples.join(" | "));
        });
});

test("a published site takes a name it publishes as a name, though it is a tag", function () {
    // (export.add_people_names: Anna is a tag of each of her pictures.)
    var list = [];
    for (var k = 1; k <= 6; k++) {
        list.push({i: k, l: "lib", t: "Beach day " + k, g: ["beach", "anna"],
                   dk: "2024-06-0" + k, ds: 1});
    }
    list.push({i: 7, l: "lib", t: "Sea turtle in a tank", g: ["turtle", "anna"],
               dk: "2024-07-01", ds: 1});
    list.push({i: 8, l: "lib", t: "Sea turtle mosaic", g: ["turtle", "mosaic"],
               dk: "2024-08-01", ds: 1});
    var page = exportPage("?q=anna+turtle", {records: list, names: ["Anna"]});
    var asked = [];
    return shown(page).then(function () {
        var core = page.context.SearchCore;
        var original = core.search;
        core.search = function (index, query, options) {
            asked.push(options && options.names);
            return original.apply(this, arguments);
        };
        return waitFor(function () {
            return chips(page).length;
        });
    }).then(function () {
        // Taken at once (no "Did you mean"), and searched by its words.
        // (assert.ok: a failed strictEqual would print the whole made-up DOM.)
        assert.ok(!byId(page, "plain-chips").querySelector(".plain-choice"),
                  "asked: " + chips(page).join(" | "));
        assert.deepStrictEqual(chips(page).slice(0, 2), ["Searching for", "Person: Anna"]);
        assert.strictEqual(count(page), "1 picture");
        // Every search of the page knows the published names (Per-term coverage).
        search(page, "anna beach");
        return waitFor(function () {
            return count(page) === "6 pictures";
        });
    }).then(function () {
        assert.ok(asked.length > 0);
        asked.forEach(function (names) {
            assert.deepStrictEqual(names, ["Anna"]);
        });
    });
});

test("a word the server gives no vector for is searched by the whole query", function () {
    var page = serverPage("?q=kowalski+turtle", {meaning: true, embedLimit: 2});
    return loaded(page).then(function () {
        return waitFor(function () {
            return page.log.embeds.length && chips(page).length;
        });
    }).then(function () {
        return new Promise(function (resolve) {
            setTimeout(resolve, 50);
        });
    }).then(function () {
        // One request; "turtle" came without a vector: search by meaning
        // stays on (the switch is there), and nothing is asked again.
        assert.deepStrictEqual(page.log.embeds, [["kowalski turtle", "kowalski", "turtle"]]);
        assert.strictEqual(byId(page, "meaning-switch").hidden, false);
        assert.strictEqual(count(page), "1 picture");
        // The words of a long query: as many texts as the server takes.
        search(page, "red car blue truck green bus yellow bike old house white barn turtle");
        return waitFor(function () {
            return page.log.embeds.length > 1;
        });
    }).then(function () {
        assert.strictEqual(page.log.embeds[1].length, 9);
    });
});

test("turned off: what is typed is searched as typed, no chips, no people", function () {
    var page = serverPage("?q=anna+at+the+beach+in+lisbon", {plain: false});
    return loaded(page).then(function () {
        return new Promise(function (resolve) {
            setTimeout(resolve, 50);
        });
    }).then(function () {
        assert.deepStrictEqual(chips(page), []);
        assert.strictEqual(count(page), "2 pictures");
        assert.ok(page.log.requests.indexOf("/api/people?hidden=1") < 0,
                  page.log.requests.join(" "));
        // The search tips leave plain language out.
        var tips = byId(page, "syntax-help").textContent;
        assert.ok(tips.indexOf("plain language") < 0, tips);
        var on = serverPage("");
        return loaded(on).then(function () {
            assert.ok(byId(on, "syntax-help").textContent.indexOf("plain language") >= 0);
        });
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
