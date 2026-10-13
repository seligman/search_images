/* Tests for People on the pages, under Node (no packages needed): the real page
   scripts on the made-up DOM of tests/js/app_meaning_test.js, with a fake fetch()
   for the server's answers. The Albums page counts smart albums that name a person
   with the people (and lets them be copied only once the people have come); the
   People page's person picker chooses with Enter the person whose name was typed,
   else a new person of that name, else the only match; and a person whose last
   faces went leaves the list in its place. All people and pictures are made up.

   Run by tests/test_people.py, or directly:
       node tests/js/people_pages_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var FAKE = require(path.join(__dirname, "app_meaning_test.js"));
var waitFor = FAKE.waitFor;
var settle = FAKE.settle;

var ROOT = path.join(__dirname, "..", "..");
var STATIC = path.join(ROOT, "static");

function read(name) {
    return fs.readFileSync(path.join(STATIC, name), "utf8");
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

/* A page of the server, as the admin sees it: template (in templates/), its scripts
   (in static/), the address's ?... part, and answer(path, body) for each request
   (a promise of a response, or null: 404). page.log.sent: [[path, body]] of the
   requests with a body. */
function serverPage(template, scripts, searchText, answer) {
    var log = {requests: [], sent: []};
    var document = FAKE.makeDocument();
    var create = document.createElement;
    document.createElement = function (tag) {
        var node = create.apply(document, arguments);
        // (What the made-up DOM leaves out: an input's empty value, select().)
        if (!node.select) {
            node.select = function () {};
        }
        if (String(tag).toLowerCase() === "input" && node.value === undefined) {
            node.value = "";
        }
        return node;
    };
    var config = {mode: "server", page: "people", api: "/api", tiles: "/tile",
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: {admin: true, static: false, people: true}};
    FAKE.parseBody(document, fs.readFileSync(path.join(ROOT, "templates", template), "utf8"),
                   config);
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
    var location = {search: searchText || "", hash: "", pathname: "/", protocol: "http:",
                    href: "http://127.0.0.1:5000/" + (searchText || ""),
                    origin: "http://127.0.0.1:5000"};
    function moved(url) {
        var parts = String(url).split("?");
        location.pathname = parts[0];
        location.search = parts.length > 1 ? "?" + parts[1] : "";
    }
    var history = {state: null, scrollRestoration: "auto", pushed: [], replaced: [],
                   pushState: function (state, title, url) {
                       this.pushed.push(url);
                       moved(url);
                   },
                   replaceState: function (state, title, url) {
                       this.replaced.push(url);
                       moved(url);
                   },
                   back: function () {}};
    function fetch(url, init) {
        log.requests.push(url);
        var body = init && init.body ? JSON.parse(init.body) : null;
        if (body) {
            log.sent.push([String(url), body]);
        }
        return answer(String(url), body) || response(404, {error: "not here"});
    }
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: false}, clipboard: null},
        location: location,
        history: history,
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
        URLSearchParams: URLSearchParams,
        URL: URL,
        fetch: fetch
    };
    context.window = context;
    context.addEventListener = function () {};
    context.removeEventListener = function () {};
    vm.createContext(context);
    scripts.forEach(function (name) {
        vm.runInContext(read(name), context, {filename: name});
    });
    return {context: context, document: document, log: log, history: history};
}

function byId(page, id) {
    return page.document.getElementById(id);
}

function dialog(page) {
    var open = page.document.querySelectorAll("dialog");
    return open[open.length - 1] || null;
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

/* ---------- The Albums page ---------------------------------------------------------- */

var RECORDS = [
    {i: 1, l: "lib", t: "Beach", g: ["beach"], ds: 1, dk: "2020-01-10"},
    {i: 2, l: "lib", t: "Garden", g: ["garden"], ds: 1, dk: "2020-01-11"},
    {i: 3, l: "lib", t: "Beach party", g: ["beach", "party"], ds: 1, dk: "2020-01-12"},
    {i: 4, l: "lib", t: "Mountain", g: ["mountain"], ds: 1, dk: "2020-01-13"}
];
var ALBUMS = [
    {id: 1, name: "With Ada", kind: "smart", cover: null, query: "person:Ada", options: {}},
    {id: 2, name: "Without Ada", kind: "smart", cover: null, query: "-person:Ada",
     options: {}},
    {id: 3, name: "Beaches", kind: "smart", cover: null, query: "beach", options: {}}
];
var PEOPLE = [{id: 5, name: "Ada", label: "Ada", cover: 11, faces: 2, ids: [1, 3]},
              {id: 6, name: "", label: "Person 6", cover: 12, faces: 1, ids: [2]},
              {id: 7, name: "Hal", label: "Hal", cover: 13, faces: 3, ids: [4],
               hidden: true}];

function albumsPage(peopleReady) {
    return serverPage("albums.html", ["site.js", "shell.js", "search-core.js", "albums.js"], "",
                      function (url) {
                          if (url.split("?")[0] === "/api/index") {
                              return response(200, {version: 1, albums: ALBUMS, records: RECORDS,
                                                    libraries: [{id: "lib",
                                                                 name: "Example"}]});
                          }
                          if (url.split("?")[0] === "/api/people") {
                              return peopleReady.then(function () {
                                  return response(200, {version: 1, people: PEOPLE});
                              });
                          }
                          return null;
                      });
}

function cardOf(page, name) {
    return page.document.querySelectorAll(".album-card").filter(function (card) {
        return card.querySelector(".album-name").textContent === name;
    })[0];
}

test("the Albums page counts a smart album that names a person with the people",
     function () {
         var release;
         var ready = new Promise(function (resolve) {
             release = resolve;
         });
         var page = albumsPage(ready);
         var copy = function (name) {
             return cardOf(page, name).querySelectorAll("button").filter(function (button) {
                 return button.textContent === "Copy to a new album";
             })[0];
         };
         return waitFor(function () {
             return cardOf(page, "With Ada");
         }).then(function () {
             // The people are on their way: nothing to copy yet (it would copy nothing,
             // or every picture).
             assert.ok(copy("With Ada").disabled);
             assert.ok(copy("Without Ada").disabled);
             assert.ok(!copy("Beaches").disabled);
             assert.ok(page.log.requests.some(function (url) {
                 return /^\/api\/people\?hidden=1$/.test(url);
             }), page.log.requests);
             release();
             return waitFor(function () {
                 return /2 pictures/.test(cardOf(page, "With Ada").textContent);
             });
         }).then(function () {
             assert.ok(/2 pictures/.test(cardOf(page, "Without Ada").textContent),
                       cardOf(page, "Without Ada").textContent);
             assert.ok(!copy("With Ada").disabled);
             copy("With Ada").click();
             return waitFor(function () {
                 return dialog(page);
             });
         }).then(function () {
             button(dialog(page), "Make the album").click();
             return waitFor(function () {
                 return page.log.sent.length;
             });
         }).then(function () {
             assert.deepStrictEqual(page.log.sent[0][1].ids.slice().sort(), [1, 3]);
         });
     });

/* ---------- The People page ---------------------------------------------------------- */

var LISTED = [{id: 3, name: "", label: "Person 3", cover: 31, faces: 4, ids: [1, 2, 3]},
              {id: 4, name: "Annabel", label: "Annabel", cover: 41, faces: 6,
               ids: [1, 2, 3, 4, 5, 6]},
              {id: 12, name: "", label: "Person 12", cover: 121, faces: 9,
               ids: [7, 8, 9, 10]},
              {id: 1, name: "", label: "Person 1", cover: 11, faces: 3, ids: [11, 12]}];

function peoplePage(searchText) {
    var gone = {};
    return serverPage("people.html", ["site.js", "shell.js", "search-core.js", "people.js"],
                      searchText, function (url, body) {
                          var plain = url.split("?")[0];
                          if (plain === "/api/people") {
                              return response(200, {version: 1, people: LISTED.filter(
                                  function (person) {
                                      return !gone[person.id];
                                  })});
                          }
                          if (plain === "/api/admin/people" && !body) {
                              return response(200, {people: {enabled: true, problem: ""},
                                                    groups: {faces: 20, grouped: 19,
                                                             people: 4, named: 1, other: 1,
                                                             not_faces: 0, due: false},
                                                    settings: {}});
                          }
                          var one = /^\/api\/admin\/people\/(\d+)$/.exec(plain);
                          if (one && !body) {
                              if (gone[one[1]]) {
                                  return response(404, {error: "There is no person."});
                              }
                              return response(200, {person: {id: Number(one[1]), name: "",
                                                             label: "Person " + one[1],
                                                             hidden: false, cover: null,
                                                             faces: 2},
                                                    faces: [{id: 301, quality: 0.9,
                                                             owner: false, image: 1},
                                                            {id: 302, quality: 0.8,
                                                             owner: false, image: 2}],
                                                    similar: []});
                          }
                          if (plain === "/api/admin/people/faces") {
                              if (body.faces === "all") {
                                  gone[body.person] = true;
                                  return response(200, {changed: 2, source: null});
                              }
                              return response(200, {person: {id: 99, name: body.name || "",
                                                             label: body.name || "Person 99",
                                                             faces: 1},
                                                    source: {id: body.person, faces: 1}});
                          }
                          return null;
                      });
}

function button(scope, text) {
    return scope.querySelectorAll("button").filter(function (node) {
        return node.textContent === text;
    })[0];
}

// Opens Move to... for the first face of the person shown; types text; presses Enter.
function moveWithEnter(page, text) {
    button(page.document.body, "Clear") && button(page.document.body, "Clear").click();
    page.document.querySelectorAll(".face-tile")[0].click();
    button(byId(page, "people-selection"), "Move to\u2026").click();
    return waitFor(function () {
        return dialog(page);
    }).then(function () {
        var input = dialog(page).querySelector("input");
        input.value = text;
        input.dispatchEvent({type: "input"});
        input.dispatchEvent({type: "keydown", key: "Enter"});
        return settle(30);
    });
}

function lastMove(page) {
    var moves = page.log.sent.filter(function (entry) {
        return entry[0] === "/api/admin/people/faces";
    });
    return moves.length ? moves[moves.length - 1][1] : null;
}

test("Enter in the person picker takes the person called so, else a new one", function () {
    var page = peoplePage("?person=3");
    return waitFor(function () {
        return page.document.querySelectorAll(".face-tile").length === 2;
    }).then(function () {
        // "Ann" is only a part of Annabel: a new person called Ann.
        return moveWithEnter(page, "Ann");
    }).then(function () {
        assert.deepStrictEqual(lastMove(page), {action: "move", faces: [301], to: null,
                                                name: "Ann", person: 3});
        return waitFor(function () {
            return page.document.querySelectorAll(".face-tile").length === 2 && !dialog(page);
        });
    }).then(function () {
        // "Person 1" is Person 1, though Person 12 (more pictures) is listed first.
        return moveWithEnter(page, "person 1");
    }).then(function () {
        assert.strictEqual(lastMove(page).to, 1);
        return waitFor(function () {
            return !dialog(page);
        });
    }).then(function () {
        // Only a part of one name: a new person still (it can be made here).
        return moveWithEnter(page, "nabe");
    }).then(function () {
        assert.deepStrictEqual([lastMove(page).to, lastMove(page).name], [null, "nabe"]);
    });
});

test("Enter merges into the only match, and into nobody when several match",
     function () {
         var page = peoplePage("?person=3");
         var sent = function () {
             return page.log.sent.filter(function (entry) {
                 return /\/merge$/.test(entry[0]);
             });
         };
         return waitFor(function () {
             return page.document.querySelectorAll(".face-tile").length === 2;
         }).then(function () {
             button(page.document.body, "Merge into\u2026").click();
             return waitFor(function () {
                 return dialog(page);
             });
         }).then(function () {
             var input = dialog(page).querySelector("input");
             input.value = "Person";
             input.dispatchEvent({type: "input"});
             input.dispatchEvent({type: "keydown", key: "Enter"});
             return settle(30);
         }).then(function () {
             assert.deepStrictEqual(sent(), []);
             assert.ok(dialog(page));
             // (No new person can be made by a merge.)
             assert.ok(!/A new person/.test(dialog(page).textContent));
             // The only one whose name has the text in it (marked as Enter's choice).
             var input = dialog(page).querySelector("input");
             input.value = "nabe";
             input.dispatchEvent({type: "input"});
             var marked = dialog(page).querySelectorAll(".is-enter");
             assert.strictEqual(marked.length, 1);
             assert.ok(/Annabel/.test(marked[0].textContent), marked[0].textContent);
             input.dispatchEvent({type: "keydown", key: "Enter"});
             return waitFor(function () {
                 return sent().length;
             });
         }).then(function () {
             assert.deepStrictEqual(sent(), [["/api/admin/people/3/merge", {into: 4}]]);
         });
     });

test("a person whose last faces went gives its place to the list", function () {
    var page = peoplePage("?person=3");
    return waitFor(function () {
        return page.document.querySelectorAll(".face-tile").length === 2;
    }).then(function () {
        button(page.document.body, "None are faces").click();
        return waitFor(function () {
            return dialog(page);
        });
    }).then(function () {
        button(dialog(page), "Mark them").click();
        return waitFor(function () {
            return byId(page, "people-title").textContent === "People";
        });
    }).then(function () {
        return settle(30);
    }).then(function () {
        assert.deepStrictEqual(page.history.replaced, ["/people"]);
        assert.strictEqual(page.context.location.search, "");
        assert.ok(!/not there any more/.test(byId(page, "people-notices").textContent),
                  byId(page, "people-notices").textContent);
        assert.ok(!page.log.requests.some(function (url) {
            return /\/api\/admin\/people\/3$/.test(url) && page.log.requests
                .indexOf(url) !== page.log.requests.lastIndexOf(url);
        }), page.log.requests);
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
