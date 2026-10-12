/* Tests for search by meaning on an exported search page (static/app.js with
   static/site.js): the page as an export ships it (templates/page.html), the
   real scripts, and a made-up DOM just good enough for them; the model is a
   stub engine (EMBED) whose query vector is that of a picture with none of
   the words typed, so only meaning can find it.

   Run by tests/test_ui_scripts.py, or directly:
       node tests/js/app_meaning_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var ROOT = path.join(__dirname, "..", "..");
var STATIC = path.join(ROOT, "static");
var PAGE = fs.readFileSync(path.join(ROOT, "templates", "page.html"), "utf8");
var SCRIPTS = ["site.js", "shell.js", "search-core.js", "viewer.js", "app.js"].map(function (name) {
    return {name: name, code: fs.readFileSync(path.join(STATIC, name), "utf8")};
});
var MODEL_ID = "m-0123456789ab";
var DIMS = 32;

/* ---------- A made-up DOM ------------------------------------------------------- */

function Node(document, tag) {
    this.ownerDocument = document;
    this.tagName = tag ? tag.toUpperCase() : "";
    this.nodeType = tag ? 1 : 3;
    this.childNodes = [];
    this.parentNode = null;
    this.attrs = {};
    this.listeners = {};
    this.classes = [];
    this.style = {setProperty: function () {}, removeProperty: function () {}};
    this.hidden = false;
    this.text = "";
}

Object.defineProperties(Node.prototype, {
    className: {
        get: function () {
            return this.classes.join(" ");
        },
        set: function (value) {
            this.classes = String(value || "").split(/\s+/).filter(Boolean);
        }
    },
    id: {
        get: function () {
            return this.attrs.id || "";
        },
        set: function (value) {
            this.attrs.id = String(value);
        }
    },
    children: {
        get: function () {
            return this.childNodes.filter(function (node) {
                return node.nodeType === 1;
            });
        }
    },
    firstChild: {
        get: function () {
            return this.childNodes[0] || null;
        }
    },
    lastChild: {
        get: function () {
            return this.childNodes[this.childNodes.length - 1] || null;
        }
    },
    nextSibling: {
        get: function () {
            var list = this.parentNode ? this.parentNode.childNodes : [];
            return list[list.indexOf(this) + 1] || null;
        }
    },
    textContent: {
        get: function () {
            return this.nodeType === 3 ? this.text : this.childNodes.map(function (node) {
                return node.textContent;
            }).join("");
        },
        set: function (value) {
            if (this.nodeType === 3) {
                this.text = String(value);
                return;
            }
            this.childNodes.forEach(function (node) {
                node.parentNode = null;
            });
            this.childNodes = [];
            if (value !== "" && value !== null && value !== undefined) {
                this.appendChild(this.ownerDocument.createTextNode(String(value)));
            }
        }
    },
    classList: {
        get: function () {
            var node = this;
            return {
                add: function () {
                    Array.prototype.forEach.call(arguments, function (name) {
                        if (node.classes.indexOf(name) < 0) {
                            node.classes.push(name);
                        }
                    });
                },
                remove: function () {
                    Array.prototype.forEach.call(arguments, function (name) {
                        node.classes = node.classes.filter(function (other) {
                            return other !== name;
                        });
                    });
                },
                toggle: function (name, on) {
                    var has = node.classes.indexOf(name) >= 0;
                    var want = on === undefined ? !has : Boolean(on);
                    if (want && !has) {
                        node.classes.push(name);
                    } else if (!want && has) {
                        this.remove(name);
                    }
                    return want;
                },
                contains: function (name) {
                    return node.classes.indexOf(name) >= 0;
                }
            };
        }
    },
    clientWidth: {get: function () {
        return 1000;
    }},
    clientHeight: {get: function () {
        return 800;
    }},
    offsetWidth: {get: function () {
        return 1000;
    }},
    offsetHeight: {get: function () {
        return 40;
    }},
    scrollHeight: {get: function () {
        return 800;
    }}
});

Node.prototype.setAttribute = function (name, value) {
    if (name === "class") {
        this.className = value;
    } else {
        this.attrs[name] = String(value);
    }
};
Node.prototype.getAttribute = function (name) {
    if (name === "class") {
        return this.className;
    }
    return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
};
Node.prototype.hasAttribute = function (name) {
    return this.getAttribute(name) !== null;
};
Node.prototype.removeAttribute = function (name) {
    delete this.attrs[name];
};
Node.prototype.appendChild = function (child) {
    if (child.isFragment) {
        child.childNodes.slice().forEach(this.appendChild, this);
        return child;
    }
    if (child.parentNode) {
        child.parentNode.removeChild(child);
    }
    child.parentNode = this;
    this.childNodes.push(child);
    if (this.ownerDocument.onAppend) {
        this.ownerDocument.onAppend(child, this);
    }
    return child;
};
Node.prototype.append = function () {
    Array.prototype.forEach.call(arguments, function (child) {
        this.appendChild(typeof child === "string" ? this.ownerDocument.createTextNode(child)
            : child);
    }, this);
};
Node.prototype.insertBefore = function (child, before) {
    if (!before) {
        return this.appendChild(child);
    }
    if (child.parentNode) {
        child.parentNode.removeChild(child);
    }
    child.parentNode = this;
    this.childNodes.splice(this.childNodes.indexOf(before), 0, child);
    return child;
};
Node.prototype.removeChild = function (child) {
    var at = this.childNodes.indexOf(child);
    if (at >= 0) {
        this.childNodes.splice(at, 1);
    }
    child.parentNode = null;
    return child;
};
Node.prototype.replaceChild = function (child, old) {
    this.insertBefore(child, old);
    return this.removeChild(old);
};
Node.prototype.remove = function () {
    if (this.parentNode) {
        this.parentNode.removeChild(this);
    }
};
Node.prototype.contains = function (other) {
    for (var node = other; node; node = node.parentNode) {
        if (node === this) {
            return true;
        }
    }
    return false;
};
Node.prototype.addEventListener = function (name, listener) {
    (this.listeners[name] = this.listeners[name] || []).push(listener);
};
Node.prototype.removeEventListener = function (name, listener) {
    this.listeners[name] = (this.listeners[name] || []).filter(function (other) {
        return other !== listener;
    });
};
Node.prototype.dispatchEvent = function (event) {
    event.target = event.target || this;
    event.preventDefault = event.preventDefault || function () {
        event.defaultPrevented = true;
    };
    event.stopPropagation = event.stopPropagation || function () {};
    for (var node = this; node; node = node.parentNode) {
        event.currentTarget = node;
        (node.listeners[event.type] || []).slice().forEach(function (listener) {
            listener.call(node, event);
        });
    }
    return !event.defaultPrevented;
};
Node.prototype.click = function () {
    if (!this.disabled) {
        this.dispatchEvent({type: "click", button: 0});
    }
};
Node.prototype.focus = function () {
    this.ownerDocument.activeElement = this;
};
Node.prototype.blur = function () {
    this.ownerDocument.activeElement = this.ownerDocument.body;
};
Node.prototype.scrollIntoView = function () {};
Node.prototype.getBoundingClientRect = function () {
    return {top: 0, left: 0, right: 1000, bottom: 40, width: 1000, height: 40};
};
Node.prototype.matches = function (selector) {
    return matches(this, selector);
};
Node.prototype.closest = function (selector) {
    for (var node = this; node && node.nodeType === 1; node = node.parentNode) {
        if (matches(node, selector)) {
            return node;
        }
    }
    return null;
};
Node.prototype.querySelectorAll = function (selector) {
    var found = [];
    (function walk(node) {
        node.childNodes.forEach(function (child) {
            if (child.nodeType === 1) {
                if (matches(child, selector)) {
                    found.push(child);
                }
                walk(child);
            }
        });
    })(this);
    return found;
};
Node.prototype.querySelector = function (selector) {
    return this.querySelectorAll(selector)[0] || null;
};

// Selectors: lists of descendant chains of tag, #id, .class and [attr=value].
function matchesSimple(node, part) {
    var re = /([#.]?[A-Za-z0-9_-]+|\[[^\]]+\]|:not\([^)]*\))/g;
    var token;
    while ((token = re.exec(part)) !== null) {
        var text = token[1];
        if (text.indexOf(":not(") === 0) {
            continue;
        }
        if (text.charAt(0) === "#") {
            if (node.id !== text.slice(1)) {
                return false;
            }
        } else if (text.charAt(0) === ".") {
            if (node.classes.indexOf(text.slice(1)) < 0) {
                return false;
            }
        } else if (text.charAt(0) === "[") {
            var attr = /^\[([^=\]]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(text);
            var value = node.getAttribute(attr[1]);
            if (value === null || (attr[2] !== undefined && value !== attr[2])) {
                return false;
            }
        } else if (node.tagName !== text.toUpperCase()) {
            return false;
        }
    }
    return true;
}

function matches(node, selector) {
    return selector.split(",").some(function (alternative) {
        var chain = alternative.trim().split(/\s+/);
        if (!matchesSimple(node, chain[chain.length - 1])) {
            return false;
        }
        var at = chain.length - 2;
        for (var up = node.parentNode; up && at >= 0; up = up.parentNode) {
            if (up.nodeType === 1 && matchesSimple(up, chain[at])) {
                at -= 1;
            }
        }
        return at < 0;
    });
}

function makeDocument() {
    var document = {
        listeners: {},
        createElement: function (tag) {
            return new Node(document, tag);
        },
        createTextNode: function (text) {
            var node = new Node(document, "");
            node.text = String(text);
            return node;
        },
        createDocumentFragment: function () {
            var node = new Node(document, "fragment");
            node.isFragment = true;
            return node;
        },
        addEventListener: Node.prototype.addEventListener,
        removeEventListener: Node.prototype.removeEventListener,
        dispatchEvent: Node.prototype.dispatchEvent,
        hidden: false,
        referrer: "",
        title: "",
        readyState: "complete"
    };
    document.documentElement = document.createElement("html");
    document.head = document.createElement("head");
    document.body = document.createElement("body");
    document.documentElement.appendChild(document.head);
    document.documentElement.appendChild(document.body);
    document.activeElement = document.body;
    document.getElementById = function (id) {
        return document.documentElement.querySelector("#" + id);
    };
    document.querySelector = function (selector) {
        return document.documentElement.querySelector(selector);
    };
    document.querySelectorAll = function (selector) {
        return document.documentElement.querySelectorAll(selector);
    };
    document.childNodes = [];
    document.parentNode = null;
    return document;
}

// The body of templates/page.html as made-up nodes (its scripts left out).
function parseBody(document, html, config) {
    var body = html.slice(html.indexOf("<body"), html.indexOf("</body>"));
    var stack = [document.body];
    var re = /<(\/?)([a-zA-Z0-9]+)([^>]*)>|([^<]+)/g;
    var token;
    var voids = {input: 1, br: 1, img: 1, meta: 1, link: 1};
    while ((token = re.exec(body)) !== null) {
        var top = stack[stack.length - 1];
        if (token[4] !== undefined) {
            if (token[4].trim()) {
                top.appendChild(document.createTextNode(token[4].replace(/&#8776;/g, "\u2248")
                    .replace(/&times;/g, "\u00d7")));
            }
            continue;
        }
        var tag = token[2].toLowerCase();
        if (tag === "body") {
            continue;
        }
        if (token[1]) {
            stack.pop();
            continue;
        }
        var node = document.createElement(tag);
        var attrRe = /([a-zA-Z-]+)(?:="([^"]*)")?/g;
        var attr;
        while ((attr = attrRe.exec(token[3])) !== null) {
            var value = attr[2] === undefined ? "" : attr[2];
            if (attr[1] === "hidden") {
                node.hidden = true;
            } else if (attr[1] === "checked") {
                node.checked = true;
            } else {
                node.setAttribute(attr[1], value);
            }
            if (attr[1] === "type") {
                node.type = value;
            }
        }
        if (tag === "script") {
            if (node.getAttribute("type") === "application/json") {
                node.textContent = JSON.stringify(config);
                top.appendChild(node);
            }
            re.lastIndex = body.indexOf("</script>", re.lastIndex) + "</script>".length;
            continue;
        }
        top.appendChild(node);
        if (!voids[tag] && !/\/$/.test(token[3])) {
            stack.push(node);
        }
    }
}

/* ---------- The exported page ------------------------------------------------- */

function unit(values) {
    var norm = Math.sqrt(values.reduce(function (sum, value) {
        return sum + value * value;
    }, 0));
    return values.map(function (value) {
        return Math.round(value / norm * 127);
    });
}

/* 60 pictures with words and meaning vectors; "Snow angel" (pic7) has none
   of the words searched for, and the stub engine's query vector is its own:
   far closer to it than to any other picture. */
function records() {
    var list = [];
    var vectors = [];
    for (var k = 0; k < 60; k++) {
        var values = [k === 7 ? 3 : 0];
        for (var d = 1; d < DIMS; d++) {
            // (Made-up numbers in -0.5..0.5: pictures unlike each other.)
            var x = Math.sin(k * 12.9898 + d * 78.233) * 43758.5453;
            values.push(x - Math.floor(x) - 0.5);
        }
        list.push({i: "pic" + k, l: "lib", t: k === 7 ? "Snow angel" : "Picture " + k,
                   d: k === 7 ? "Someone lying in fresh snow." : "A picture of things " + k,
                   g: k === 7 ? ["snow"] : ["thing"], ds: 1, dk: "2020-01-" + (10 + k % 20)});
        vectors.push(unit(values));
    }
    return {records: list, vectors: vectors};
}

function base64(numbers) {
    return Buffer.from(Int8Array.from(numbers).buffer).toString("base64");
}

/* A fresh exported search page. options: {meaning (false: an export without
   its model), saveData, check (the self-check's cosine), fail (the model
   cannot load), words (Words chosen on an earlier visit)}. page.log lists
   the scripts added. */
function exportPage(options) {
    options = options || {};
    var data = records();
    var log = {scripts: [], progress: 0, loads: 0};
    var document = makeDocument();
    var config = {mode: "static", page: "search", data: "data/", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: {admin: false, static: true, images: false}, title: "Example"};
    parseBody(document, PAGE, config);
    var storage = function () {
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
            },
            items: items
        };
    };
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: Boolean(options.saveData)},
                    clipboard: null},
        location: {search: "", hash: "", pathname: "/x/index.html", protocol: "file:",
                   href: "file:///x/index.html", origin: "null"},
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
        console: {log: function () {}, warn: function (text) {
            log.warning = String(text);
        }, error: function (text) {
            log.error = String(text);
        }},
        atob: function (text) {
            return Buffer.from(text, "base64").toString("binary");
        },
        btoa: function (text) {
            return Buffer.from(text, "binary").toString("base64");
        },
        URLSearchParams: URLSearchParams,
        URL: URL,
        SEARCH_DATA_MANIFEST: {format: 1, version: 3, count: data.records.length,
                               shards: ["index-000.js"], details: [], vectors: ["vectors-000.js"],
                               library: {id: "lib", name: "Example"},
                               libraries: [{id: "lib", name: "Example"}], capabilities: {},
                               meaning: options.meaning === false ? undefined : {
                                   id: MODEL_ID, dims: DIMS, model: "meaning/model.js",
                                   parts: ["meaning/w-000.js"], bytes: 2000000,
                                   gzip: 25000000, license: "meaning/LICENSE.txt", credit: "",
                                   assets: {}}}
    };
    context.window = context;
    if (options.words) {
        context.localStorage.setItem("searchImages.by", "words");
    }
    context.addEventListener = function () {};
    context.removeEventListener = function () {};
    var engine = {
        unsupported: function () {
            return options.unsupported || "";
        },
        load: function (meaning, settings) {
            log.loads += 1;
            if (options.fail) {
                return Promise.reject(new Error("a part of the model is damaged"));
            }
            return new Promise(function (resolve) {
                settings.progress(0, 100);
                setTimeout(function () {
                    settings.progress(40, 100);
                    log.progress += 1;
                    setTimeout(function () {
                        settings.progress(100, 100);
                        resolve({
                            check: function () {
                                return options.check === undefined ? 0.999 : options.check;
                            },
                            query: function (text) {
                                log.query = text;
                                // (options.far: a query like none of the pictures.)
                                return Int8Array.from(options.far ? unit([-1].concat(
                                    new Array(DIMS - 1).fill(0))) : data.vectors[7]);
                            }
                        });
                    }, 30);
                }, 30);
            });
        }
    };
    // Scripts added to the page run (as theirs would) a moment later.
    document.onAppend = function (node) {
        if (node.tagName !== "SCRIPT") {
            return;
        }
        var src = String(node.src || "");
        log.scripts.push(src.split("?")[0]);
        setTimeout(function () {
            // (options.missing: the scripts that are not there.)
            if (options.missing && options.missing.test(src)) {
                if (node.onerror) {
                    node.onerror();
                }
                return;
            }
            if (/index-000\.js/.test(src)) {
                context.SEARCH_DATA.push(data.records);
            } else if (/vectors-000\.js/.test(src)) {
                context.SEARCH_VECTORS.push({dims: DIMS, model: MODEL_ID,
                                             ids: data.records.map(function (rec) {
                                                 return rec.i;
                                             }),
                                             data: base64([].concat.apply([], data.vectors))});
            } else if (/embed-engine\.js/.test(src)) {
                context.EMBED = engine;
            }
            if (node.onload) {
                node.onload();
            }
        }, 5);
    };
    vm.createContext(context);
    SCRIPTS.forEach(function (script) {
        vm.runInContext(script.code, context, {filename: script.name});
    });
    return {context: context, document: document, log: log, data: data};
}

function settle(ms) {
    return new Promise(function (resolve) {
        setTimeout(resolve, ms || 0);
    });
}

function waitFor(check, timeout) {
    var end = Date.now() + (timeout || 3000);
    return new Promise(function (resolve, reject) {
        (function poll() {
            var value = check();
            if (value) {
                resolve(value);
            } else if (Date.now() > end) {
                reject(new Error("timed out waiting for " + check));
            } else {
                setTimeout(poll, 5);
            }
        })();
    });
}

// The page has shown its pictures.
function loaded(page) {
    return waitFor(function () {
        return /\d+ pictures?/.test(byId(page, "result-count").textContent);
    });
}

function byId(page, id) {
    return page.document.getElementById(id);
}

// Types a query and presses Enter, as a visitor does.
function search(page, text) {
    var input = byId(page, "query");
    input.value = text;
    input.dispatchEvent({type: "input"});
    input.dispatchEvent({type: "keydown", key: "Enter"});
}

function notes(page) {
    return byId(page, "result-notes").textContent;
}

function shown(page) {
    return byId(page, "grid").querySelectorAll(".tile").map(function (tile) {
        return tile.textContent;
    });
}

function engineScripts(page) {
    return page.log.scripts.filter(function (src) {
        return /embed-|meaning\//.test(src);
    });
}

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

/* ---------- Tests ------------------------------------------------------------------- */

test("the switch shows before anything of the model loads", function () {
    var page = exportPage();
    return loaded(page).then(function () {
        assert.strictEqual(byId(page, "meaning-switch").hidden, false);
        assert.deepStrictEqual(engineScripts(page), []);
        assert.ok(page.log.scripts.indexOf("vectors-000.js") < 0 &&
                  page.log.scripts.indexOf("data/vectors-000.js") < 0,
                  "no vectors before a search");
        // Without the model in the export: no switch (as before).
        var plain = exportPage({meaning: false});
        return loaded(plain).then(function () {
            assert.strictEqual(byId(plain, "meaning-switch").hidden, true);
        });
    });
});

test("a search loads the vectors, then the model, with its progress; then it runs again",
     function () {
         var page = exportPage();
         var seen = [];
         return loaded(page).then(function () {
             search(page, "kids playing outside");
             return waitFor(function () {
                 seen.push(notes(page));
                 return /found by meaning/.test(notes(page));
             });
         }).then(function () {
             var order = page.log.scripts.map(function (src) {
                 return src.replace(/^.*\//, "");
             });
             assert.ok(order.indexOf("vectors-000.js") < order.indexOf("embed-engine.js"),
                       order.join(" "));
             assert.ok(seen.some(function (text) {
                 return /word matches for now: loading search by meaning \(\d+ of 24 MB\)/
                     .test(text);
             }), seen.join(" | "));
             assert.strictEqual(page.log.query, "kids playing outside");
             assert.deepStrictEqual(shown(page).filter(function (text) {
                 return /Snow angel/.test(text);
             }).length, 1);
             assert.ok(byId(page, "grid").querySelector(".badge-meaning"));
             // The query's vector is kept for the tab, by model.
             var kept = JSON.parse(page.context.sessionStorage.getItem(
                 "searchImages.queryVectors"));
             assert.deepStrictEqual(Object.keys(kept), [MODEL_ID + "\nkids playing outside"]);
             assert.strictEqual(kept[MODEL_ID + "\nkids playing outside"].model, MODEL_ID);
         });
     });

test("a query no word matches: no spelling offered that only meaning would find", function () {
    // "playing" is two edits from "lying" (in the snow angel's description),
    // but "kids lying outside" has no word match either.
    var page = exportPage();
    return loaded(page).then(function () {
        search(page, "kids playing outside");
        return waitFor(function () {
            return /found by meaning/.test(notes(page));
        });
    }).then(function () {
        assert.ok(/closest in meaning/.test(byId(page, "meaning-notes").textContent),
                  byId(page, "meaning-notes").textContent);
        assert.ok(!/Did you mean/.test(byId(page, "meaning-notes").textContent),
                  byId(page, "meaning-notes").textContent);
        // A real typo is still offered.
        search(page, "snwo");
        return waitFor(function () {
            return /Did you mean snow\?/.test(byId(page, "meaning-notes").textContent);
        });
    });
});

test("nothing has the words, nor is close in meaning: the empty page says so", function () {
    var page = exportPage({far: true});
    return loaded(page).then(function () {
        search(page, "kids playing outside today");
        return waitFor(function () {
            return !byId(page, "empty").hidden &&
                /close enough in meaning/.test(byId(page, "empty").textContent);
        });
    }).then(function () {
        assert.ok(!/try fewer or more general words/.test(byId(page, "empty").textContent),
                  byId(page, "empty").textContent);
    });
});

test("choosing Words never loads the model", function () {
    var page = exportPage({words: true});
    return loaded(page).then(function () {
        var pressed = byId(page, "meaning-switch").querySelectorAll("button").filter(function (b) {
            return b.getAttribute("aria-pressed") === "true";
        });
        assert.deepStrictEqual(pressed.map(function (b) {
            return b.getAttribute("data-by");
        }), ["words"]);
        search(page, "kids playing outside");
        return settle(300);
    }).then(function () {
        assert.deepStrictEqual(engineScripts(page), []);
        assert.ok(!/found by meaning|loading/.test(notes(page)), notes(page));
    });
});

// Counts the page's requests for query vectors.
function countAsks(page) {
    var count = {asks: 0};
    var ask = page.context.SITE.queryVector;
    page.context.SITE.queryVector = function () {
        count.asks += 1;
        return ask.apply(this, arguments);
    };
    return count;
}

test("a model that cannot load: word matches, with a note", function () {
    var page = exportPage({fail: true});
    var count;
    return loaded(page).then(function () {
        count = countAsks(page);
        search(page, "kids playing outside");
        return waitFor(function () {
            return /could not load/.test(notes(page));
        });
    }).then(function () {
        return settle(300);
    }).then(function () {
        assert.ok(/word matches only: search by meaning could not load/.test(notes(page)),
                  notes(page));
        assert.strictEqual(byId(page, "meaning-switch").hidden, false);
        // Nothing found: the page says why, and does not ask again and again
        // (it renders the empty results more than once).
        assert.ok(/Search by meaning could not load, so only the words were looked for/
            .test(byId(page, "empty").textContent), byId(page, "empty").textContent);
        assert.ok(count.asks <= 2, String(count.asks));
        // The next query asks again (and hears the same).
        search(page, "children in the snow");
        return settle(300);
    }).then(function () {
        assert.ok(count.asks <= 4, String(count.asks));
        assert.strictEqual(page.log.loads, 1);
    });
});

test("a model that fails its self-check hides the switch, with a note", function () {
    var page = exportPage({check: 0.4});
    return loaded(page).then(function () {
        search(page, "kids playing outside");
        return waitFor(function () {
            return byId(page, "meaning-switch").hidden;
        });
    }).then(function () {
        assert.ok(/self-check 0\.400/.test(page.log.warning), page.log.warning);
        assert.ok(!/loading|found by meaning/.test(notes(page)), notes(page));
        assert.ok(/word matches only: search by meaning does not work in this browser/
            .test(notes(page)), notes(page));
        // The next search says so too, without loading anything again.
        search(page, "children in the snow");
        return settle(100);
    }).then(function () {
        assert.strictEqual(page.log.loads, 1);
        assert.ok(/does not work in this browser/.test(notes(page)), notes(page));
        // Words alone: no note.
        byId(page, "meaning-switch").querySelectorAll("button")[0].click();
        return settle(50);
    }).then(function () {
        assert.ok(!/does not work/.test(notes(page)), notes(page));
    });
});

test("a browser the engine cannot run in: word matches, with a note", function () {
    var page = exportPage({unsupported: "this browser is too old"});
    return loaded(page).then(function () {
        search(page, "kids playing outside");
        return waitFor(function () {
            return /does not work in this browser/.test(notes(page));
        });
    }).then(function () {
        assert.strictEqual(byId(page, "meaning-switch").hidden, true);
        assert.strictEqual(page.log.loads, 0, "no model downloaded");
        assert.ok(/too old/.test(page.log.warning), page.log.warning);
    });
});

test("vectors that do not load: the model is not downloaded", function () {
    var page = exportPage({missing: /vectors-000\.js/});
    return loaded(page).then(function () {
        search(page, "kids playing outside");
        return waitFor(function () {
            return /could not load/.test(notes(page));
        });
    }).then(function () {
        return settle(200);
    }).then(function () {
        assert.strictEqual(page.log.loads, 0);
        assert.deepStrictEqual(engineScripts(page), []);
        assert.ok(/word matches only: search by meaning could not load/.test(notes(page)),
                  notes(page));
        assert.ok(!/loading/.test(notes(page)), notes(page));
        assert.ok(/could not load, so only the words/.test(byId(page, "empty").textContent),
                  byId(page, "empty").textContent);
    });
});

test("without its WebAssembly kernel the model still loads", function () {
    var page = exportPage({missing: /embed-kernel\.js/});
    return loaded(page).then(function () {
        search(page, "kids playing outside");
        return waitFor(function () {
            return /found by meaning/.test(notes(page));
        });
    }).then(function () {
        assert.strictEqual(page.log.loads, 1);
    });
});

test("with Save-Data on, a button downloads the model", function () {
    var page = exportPage({saveData: true});
    return loaded(page).then(function () {
        search(page, "kids playing outside");
        return waitFor(function () {
            return byId(page, "result-notes").querySelector(".meaning-download");
        });
    }).then(function (button) {
        assert.strictEqual(button.textContent, "Search by meaning (24 MB)");
        assert.deepStrictEqual(engineScripts(page), []);
        // The empty results say why, with the same button.
        assert.ok(/downloads 24 MB once/.test(byId(page, "empty").textContent),
                  byId(page, "empty").textContent);
        return settle(100).then(function () {
            assert.strictEqual(page.log.loads, 0);
            button.click();
            return waitFor(function () {
                return /found by meaning/.test(notes(page));
            });
        });
    }).then(function () {
        assert.strictEqual(page.context.localStorage.getItem("searchImages.meaningDownload"),
                           "1");
    });
});

/* ---------- Run ---------------------------------------------------------------------- */

// (tests/js/app_looks_test.js borrows the made-up DOM above; these tests
// do not run then.)
if (require.main !== module) {
    module.exports = {makeDocument: makeDocument, parseBody: parseBody, waitFor: waitFor,
                      settle: settle, byId: byId, search: search, notes: notes,
                      PAGE: PAGE, SCRIPTS: SCRIPTS};
    return;
}

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
