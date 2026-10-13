/* The Albums page (/albums): the owner's albums (albums.py), each with its
   cover and how many pictures it shows, a link that opens it in the search
   page (album:"Name"), and -- for the admin -- New album, Rename, Delete
   (the pictures stay), "Copy to a new album" (a smart album's pictures as
   they are now, in a manual album: an export can be limited to that) and,
   for a manual album, Arrange (?album=<id>): its pictures in their order,
   moved by dragging them (mouse or pen), by the arrow buttons on each
   picture, or from the keyboard (the arrow keys go from picture to
   picture; Alt+arrow keys move it; Alt+Home and Alt+End to the start and
   the end; C makes it the cover; Delete takes it out, with Undo). The
   search page shows a search of one album in this order. "Change its
   search" opens a smart album's search with ?album=<id>, so the search
   page's Save offers to update that album.

   Everyone else sees the albums and opens them; nothing changes. The
   pictures and the albums come from SITE.loadIndex, and the counts and
   covers from SearchCore (a smart album's search runs here, by meaning
   too when the server can embed it, and with the people of
   SITE.peopleForSearch when it names a person), so they agree with the
   search page. A smart album that names a person cannot be copied until
   the people have come (nor where there are none to have).

   Pages are built with DOM calls and textContent only (never innerHTML).
*/
(function () {
    "use strict";

    var el = SHELL.el;
    var clear = SHELL.clear;
    var fmt = SHELL.format;
    var adminOn = Boolean(SITE.capabilities.admin) && !SITE.capabilities.static;
    // Pictures one request names at most (albums.MARK_LIMIT).
    var CHUNK = 10000;
    // A smart album's meaning vector is asked for again this often while
    // the server's model starts (as the search page does).
    var MEANING_RETRY = 3000;
    var MEANING_TRIES = 60;

    function byId(id) {
        return document.getElementById(id);
    }

    var data = null;            // {records, byId, index, albums}
    var vectors = null;
    var queryVectors = {};      // meaning text -> vector (Int8Array) or false (none)
    var vectorTries = {};       // meaning text -> how often it was asked for (-1: asking)
    var looksAsked = false;
    // The people for person: in smart albums (the admin's hidden ones too, as the
    // search page has them); the list is drawn again when they come.
    var peopleSearch = SITE.peopleForSearch(function () {
        render();
    }, adminOn);
    var PERSON_TERM = /(^|\s)-?person:/i;

    /* ---------- Albums and their pictures ---------------------------------- */

    // What album: names an album with in a search (by its id when its
    // name cannot say it).
    function albumTerm(album) {
        return SearchCore.albumTerm(data.albums, album);
    }

    /* The meaning vector of a smart album's search, asked for of the
       server; while its model starts, asked again every MEANING_RETRY
       (MEANING_TRIES times), and the list drawn again when it comes. */
    function vectorFor(text) {
        if (!vectors || !vectors.query || !text) {
            return null;
        }
        if (queryVectors[text] !== undefined) {
            return queryVectors[text] || null;
        }
        var tries = vectorTries[text] || 0;
        if (tries < 0) {
            return null;                        // (asking now)
        }
        vectorTries[text] = -1;
        SITE.queryVector(text, function (answer) {
            answer = answer || {};
            var vector = answer.state === "ready" && vectors && answer.model === vectors.model &&
                answer.dims === vectors.dims ? SearchCore.queryVector(answer.vec, vectors.dims)
                : null;
            if (vector) {
                queryVectors[text] = vector;
                render();
            } else if ((answer.state === "starting" || answer.state === "busy") &&
                       tries + 1 < MEANING_TRIES) {
                vectorTries[text] = tries + 1;
                window.setTimeout(render, MEANING_RETRY);
            } else {
                queryVectors[text] = false;     // (by its words)
            }
        });
        return null;
    }

    // Smart albums that look for pictures like one (look:) need the look
    // vectors: loaded once, then the list is drawn again.
    function wantLooks() {
        if (looksAsked || !data.albums.some(function (album) {
            return album.kind === "smart" && /(^|\s)-?looks?:/i.test(album.query || "");
        })) {
            return;
        }
        looksAsked = true;
        SITE.attachLooks(data.index, function (looks) {
            if (looks) {
                render();
            }
        });
    }

    // The pictures an album shows, in its order (as the search page has
    // them; a drive found by its own words with all of its clips).
    function picturesOf(album) {
        var result = SearchCore.search(data.index, albumTerm(album), peopleSearch.options({
            albums: data.albums, libraries: [], hidden: false, sort: "album", group: false,
            vectorFor: vectorFor, wholeDrives: true
        }));
        return result.items;
    }

    // Smart albums that name a person need the people: asked for once.
    function wantPeople() {
        peopleSearch.want(data.albums.filter(function (album) {
            return album.kind === "smart" && typeof album.query === "string";
        }).map(function (album) {
            return album.query;
        }).join(" "));
    }

    /* Why a smart album's pictures cannot be copied now ('' when they can):
       it names a person and the people have not come (or there are none). */
    function notCopyable(album) {
        if (!PERSON_TERM.test(album.query || "")) {
            return "";
        }
        if (peopleSearch.pending()) {
            return "Wait a moment: the people it looks for are on their way.";
        }
        return peopleSearch.options({}).people ? ""
            : "The people it looks for are not available here.";
    }

    function coverOf(album, items) {
        if (album.cover !== null && album.cover !== undefined && data.byId[album.cover] &&
                items.indexOf(album.cover) >= 0) {
            return album.cover;
        }
        return items.length ? items[0] : null;
    }

    function searchUrl(album) {
        return SITE.searchUrl({q: albumTerm(album)});
    }

    /* The search of a smart album, as it was saved ("Change its search";
       for the admin with album=<id>, so the search page offers to update
       the album). */
    function savedSearchUrl(album) {
        var options = album.options || {};
        var url = SITE.searchUrl({q: album.query || "", filters: options.filters || {},
                                  sort: options.sort || "", by: options.by || "",
                                  lib: options.lib || "", from: options.from || "",
                                  to: options.to || ""});
        if (!adminOn) {
            return url;
        }
        var hash = url.indexOf("#");
        var base = hash >= 0 ? url.slice(0, hash) : url;
        return base + (base.indexOf("?") >= 0 ? "&" : "?") + "album=" +
            encodeURIComponent(album.id) + (hash >= 0 ? url.slice(hash) : "");
    }

    // A smart album's search in words: 'sunset beach, 2 filters, newest first'.
    function searchText(album) {
        var options = album.options || {};
        var parts = [];
        if (album.query) {
            parts.push("\u201c" + album.query + "\u201d");
        }
        var filters = 0;
        Object.keys(options.filters || {}).forEach(function (key) {
            filters += (options.filters[key] || []).length;
        });
        if (filters) {
            parts.push(fmt.plural(filters, "filter"));
        }
        if (options.from || options.to) {
            parts.push(SearchCore.rangeLabel(options.from || "", options.to || ""));
        }
        if (options.by === "meaning") {
            parts.push("by meaning");
        } else if (options.by === "words") {
            parts.push("words only");
        }
        return parts.join(", ");
    }

    /* ---------- The list -------------------------------------------------------- */

    function notice(kind, text) {
        var box = byId("albums-notices");
        clear(box);
        if (text) {
            box.appendChild(SHELL.notice(kind, text));
        }
    }

    function card(album) {
        var items = picturesOf(album);
        var cover = coverOf(album, items);
        var smart = album.kind === "smart";
        var node = el("article", {className: "card album-card" + (smart ? " is-smart" : "")});
        var link = el("a", {className: "album-cover", href: searchUrl(album),
                            attrs: {"aria-label": "Open " + album.name}});
        if (cover !== null) {
            link.appendChild(el("img", {src: SITE.thumbUrl(data.byId[cover] || cover), alt: "",
                                        loading: "lazy"}));
        } else {
            link.appendChild(el("span", {className: "album-empty", text: smart ? "No matches"
                : "Empty"}));
        }
        node.appendChild(link);
        var head = el("div", {className: "album-head"});
        head.appendChild(el("h2", {className: "album-name"}, [
            el("a", {href: searchUrl(album), text: album.name})
        ]));
        var facts = el("p", {className: "album-facts"});
        facts.appendChild(document.createTextNode(fmt.plural(items.length, "picture")));
        if (smart) {
            facts.appendChild(el("span", {className: "album-kind", text: "Smart"}));
        }
        head.appendChild(facts);
        if (smart) {
            head.appendChild(el("p", {className: "album-search", text: searchText(album) ||
                                      "a saved search"}));
        }
        node.appendChild(head);
        var actions = el("div", {className: "album-actions"});
        actions.appendChild(el("a", {className: "button small primary", href: searchUrl(album),
                                     text: "Open"}));
        if (smart) {
            actions.appendChild(el("a", {className: "button small", href: savedSearchUrl(album),
                                         text: adminOn ? "Change its search" : "Its search",
                                         title: adminOn ? "Open the saved search; change it, " +
                                             "then save it (Update) on the search page"
                                             : "Open the search it was saved from"}));
        }
        if (adminOn) {
            if (!smart) {
                actions.appendChild(el("a", {className: "button small", text: "Arrange",
                                             href: "/albums?album=" + encodeURIComponent(album.id),
                                             on: {click: function (event) {
                                                 if (event.button === 0 && !event.ctrlKey &&
                                                         !event.metaKey && !event.shiftKey) {
                                                     event.preventDefault();
                                                     go(album.id, true);
                                                 }
                                             }}}));
            } else {
                var why = notCopyable(album);
                actions.appendChild(el("button", {type: "button", className: "button small",
                                                  text: "Copy to a new album", disabled: !!why,
                                                  title: why || "A manual album with the " +
                                                      "pictures it finds now (an export can " +
                                                      "be limited to it)",
                                                  on: {click: function () {
                                                      copyAlbum(album, items);
                                                  }}}));
            }
            actions.appendChild(el("button", {type: "button", className: "button small",
                                              text: "Rename", on: {click: function () {
                                                  rename(album);
                                              }}}));
            actions.appendChild(el("button", {type: "button",
                                              className: "button small danger-text subtle-danger",
                                              text: "Delete", on: {click: function () {
                                                  remove(album, items.length);
                                              }}}));
        }
        node.appendChild(actions);
        return node;
    }

    function renderList() {
        byId("albums-title").textContent = "Albums";
        byId("albums-lead").textContent = adminOn
            ? "Pictures you put together, and searches you saved. Put pictures in an album from " +
              "the search page (the viewer's Add to album, or select them) and save a search " +
              "there as a smart album."
            : "Pictures put together, and saved searches. Open an album to see its pictures.";
        var actions = byId("albums-actions");
        clear(actions);
        if (adminOn) {
            actions.appendChild(el("button", {type: "button", className: "button primary",
                                              text: "New album", on: {click: create}}));
        }
        var body = byId("albums-body");
        clear(body);
        if (!data.albums.length) {
            body.appendChild(el("div", {className: "card album-none"}, [
                el("p", {text: adminOn ? "There are no albums yet." : "There are no albums."}),
                adminOn ? el("p", {className: "field-hint", text: "Make one with New album, or " +
                    "from the search page: open a picture and press A (Add to album), select " +
                    "pictures and choose Add to album, or search and choose Save as album."})
                    : null
            ]));
            return;
        }
        var grid = el("div", {className: "album-grid"});
        data.albums.forEach(function (album) {
            grid.appendChild(card(album));
        });
        body.appendChild(grid);
    }

    /* ---------- Changes (admin) ----------------------------------------------- */

    function nameDialog(title, value, confirmText, save) {
        var box = SHELL.modal({title: title});
        var input = el("input", {type: "text", className: "input", maxLength: 100,
                                 autocomplete: "off", spellcheck: false, value: value || ""});
        var field = SHELL.field("Album name", input);
        box.body.appendChild(field.node);
        box.button("Cancel", "", function () {
            box.close();
        });
        var ok = box.button(confirmText, "primary", go);
        box.submitOnEnter(go);
        input.addEventListener("input", function () {
            field.error.hidden = true;
        });
        function go() {
            var name = input.value.replace(/\s+/g, " ").replace(/^\s+|\s+$/g, "");
            if (!name) {
                field.error.textContent = "Give the album a name.";
                field.error.hidden = false;
                input.focus();
                return;
            }
            ok.disabled = true;
            save(name).then(function () {
                box.close();
            }, function (error) {
                ok.disabled = false;
                field.error.textContent = error && error.message ? error.message : "";
                field.error.hidden = !field.error.textContent;
            });
        }
        window.setTimeout(function () {
            input.focus();
            input.select();
        }, 0);
    }

    function create() {
        nameDialog("New album", "", "Make the album", function (name) {
            return SHELL.admin("/albums", {name: name, kind: "manual"}, {quiet: true})
                .then(function (reply) {
                    SHELL.toast("Made the album " + reply.album.name + ". Put pictures in it " +
                                "from the search page.", "ok");
                    reload();
                });
        });
    }

    function rename(album) {
        nameDialog("Rename " + album.name, album.name, "Rename", function (name) {
            return SHELL.admin("/albums/" + encodeURIComponent(album.id), {name: name},
                               {quiet: true}).then(function (reply) {
                album.name = reply.album.name;
                SHELL.toast("Renamed to " + reply.album.name + ".", "ok");
                reload();
            });
        });
    }

    function remove(album, count) {
        SHELL.confirm({
            title: "Delete the album " + album.name + "?",
            message: album.kind === "smart" ? "The saved search goes; the pictures stay."
                : "The album goes; its " + fmt.plural(count, "picture") + " stay in the " +
                  "library (and in other albums).",
            confirmText: "Delete the album",
            danger: true
        }).then(function (yes) {
            if (!yes) {
                return;
            }
            SHELL.admin("/albums/" + encodeURIComponent(album.id) + "/delete", {})
                .then(function () {
                    SHELL.toast("Deleted the album " + album.name + ".", "ok");
                    reload();
                }, function () {});
        });
    }

    function copyAlbum(album, items) {
        nameDialog("Copy " + album.name + " to a new album", album.name + " (copy)",
                   "Make the album", function (name) {
            var first = items.slice(0, CHUNK);
            return SHELL.admin("/albums", {name: name, kind: "manual", ids: first},
                               {quiet: true}).then(function (reply) {
                var chain = Promise.resolve();
                for (var k = CHUNK; k < items.length; k += CHUNK) {
                    (function (part) {
                        chain = chain.then(function () {
                            return SHELL.admin("/albums/" + encodeURIComponent(reply.album.id) +
                                               "/items", {add: part});
                        });
                    })(items.slice(k, k + CHUNK));
                }
                return chain.then(function () {
                    SHELL.toast("Made the album " + reply.album.name + " with " +
                                fmt.plural(items.length, "picture") + ".", "ok");
                    reload();
                });
            });
        });
    }

    /* ---------- Arrange a manual album -------------------------------------- */

    var arranging = null;       // {album, order: [ids], focus: index}
    var dragging = null;        // {id, startX, startY, active, target, after}

    function albumById(id) {
        return data.albums.filter(function (album) {
            return String(album.id) === String(id);
        })[0] || null;
    }

    function arrangeOrder(album) {
        // Every picture of the album this page has (hidden ones too, so the
        // order the admin sets covers them), in the album's order.
        var seen = {};
        var order = [];
        (album.items || []).forEach(function (id) {
            if (data.byId[id] && !seen[id]) {
                seen[id] = true;
                order.push(id);
            }
        });
        return order;
    }

    function columns(grid) {
        var tiles = grid.querySelectorAll(".arrange-tile");
        if (tiles.length < 2) {
            return 1;
        }
        var top = tiles[0].offsetTop;
        var count = 0;
        for (var k = 0; k < tiles.length && tiles[k].offsetTop === top; k++) {
            count += 1;
        }
        return Math.max(1, count);
    }

    // Move ids to just before the picture before (null: to the end), here
    // and on the server; focus follows the first moved picture.
    function move(ids, before) {
        var album = arranging.album;
        var rest = arranging.order.filter(function (id) {
            return ids.indexOf(id) < 0;
        });
        var at = before === null ? rest.length : rest.indexOf(before);
        if (at < 0) {
            return;
        }
        var order = rest.slice(0, at).concat(ids, rest.slice(at));
        if (order.join(",") === arranging.order.join(",")) {
            return;
        }
        arranging.order = order;
        album.items = order.slice();
        arranging.focus = order.indexOf(ids[0]);
        drawArrange(true);
        SHELL.admin("/albums/" + encodeURIComponent(album.id) + "/order",
                    {ids: ids, before: before}).then(function (reply) {
            if (Array.isArray(reply.order)) {
                album.items = reply.order.slice();
            }
            status("Saved the new order.");
        }, function () {
            reload();
        });
    }

    // Move the picture at index by step places (or to the start / the end).
    function moveBy(index, step) {
        var order = arranging.order;
        var id = order[index];
        var target = Math.max(0, Math.min(order.length - 1, index + step));
        if (target === index) {
            return;
        }
        var rest = order.filter(function (other) {
            return other !== id;
        });
        move([id], target >= rest.length ? null : rest[target]);
    }

    // A picture taken out of (or put back in) the album here.
    function placeHere(album, id, inside) {
        var rec = data.byId[id];
        if (!rec) {
            return;
        }
        var list = Array.isArray(rec.al) ? rec.al.filter(function (other) {
            return other !== album.id;
        }) : [];
        if (inside) {
            list.push(album.id);
            list.sort(function (a, b) {
                return a - b;
            });
        }
        if (list.length) {
            rec.al = list;
        } else {
            delete rec.al;
        }
    }

    /* Take a picture out of the album (the \u00d7 button, or Delete), with Undo:
       it goes back to its place (and is the cover again if it was). */
    function takeOut(id) {
        var album = arranging.album;
        var at = arranging.order.indexOf(id);
        var next = at >= 0 && at + 1 < arranging.order.length ? arranging.order[at + 1] : null;
        var wasCover = album.cover === id;
        SHELL.admin("/albums/" + encodeURIComponent(album.id) + "/items", {remove: [id]})
            .then(function () {
                arranging.order = arranging.order.filter(function (other) {
                    return other !== id;
                });
                album.items = arranging.order.slice();
                placeHere(album, id, false);
                if (wasCover) {
                    album.cover = null;
                }
                arranging.focus = Math.max(0, Math.min(arranging.focus,
                                                       arranging.order.length - 1));
                drawArrange(true);
                status("Took the picture out of the album (it stays in the library).",
                       function () {
                           putBack(album, id, next, wasCover);
                       });
            }, function () {});
    }

    // Undo of takeOut: the picture back in the album, before next (null: last).
    function putBack(album, id, next, wasCover) {
        var url = "/albums/" + encodeURIComponent(album.id);
        SHELL.admin(url + "/items", {add: [id]}).then(function () {
            var before = next !== null && arranging && arranging.order.indexOf(next) >= 0
                ? next : null;
            return SHELL.admin(url + "/order", {ids: [id], before: before});
        }).then(function (reply) {
            return wasCover ? SHELL.admin(url, {cover: id}).then(function () {
                return reply;
            }) : reply;
        }).then(function (reply) {
            placeHere(album, id, true);
            if (wasCover) {
                album.cover = id;
            }
            album.items = Array.isArray(reply.order) ? reply.order.slice()
                : (album.items || []).concat([id]);
            if (arranging && arranging.album === album) {
                arranging.order = arrangeOrder(album);
                arranging.focus = Math.max(0, arranging.order.indexOf(id));
                drawArrange(true);
                status("Put the picture back in its place.");
            }
        }, function () {
            reload();
        });
    }

    function makeCover(id) {
        var album = arranging.album;
        SHELL.admin("/albums/" + encodeURIComponent(album.id), {cover: id}).then(function () {
            album.cover = id;
            drawArrange(true);
            status("That picture is the album's cover now.");
        }, function () {});
    }

    var statusBox = null;       // the arrange view's line of what happened

    // The line of what happened, with an Undo button when undo is given.
    function status(text, undo) {
        if (!statusBox) {
            return;
        }
        clear(statusBox);
        statusBox.appendChild(document.createTextNode(text));
        if (undo) {
            statusBox.appendChild(document.createTextNode(" "));
            statusBox.appendChild(el("button", {type: "button", className: "button small",
                                                text: "Undo", on: {click: function (event) {
                                                    event.currentTarget.disabled = true;
                                                    undo();
                                                }}}));
        }
    }

    function tileButton(text, label, onClick) {
        var button = el("button", {type: "button", className: "arrange-button", text: text,
                                   title: label, attrs: {"aria-label": label}});
        button.tabIndex = -1;
        button.addEventListener("click", function (event) {
            event.stopPropagation();
            onClick();
        });
        return button;
    }

    function arrangeTile(id, index) {
        var rec = data.byId[id] || {i: id};
        var album = arranging.album;
        var cover = album.cover === id || (album.cover === null && index === 0);
        var tile = el("li", {className: "arrange-tile" + (rec.h ? " is-hidden" : "") +
                             (cover ? " is-cover" : ""),
                             attrs: {"data-id": String(id), "aria-label": (index + 1) + ". " +
                                     (rec.t || rec.n || "Picture") + (cover ? ", the cover" : "") +
                                     (rec.h ? ", hidden" : "")}});
        tile.tabIndex = index === arranging.focus ? 0 : -1;
        var image = el("img", {src: SITE.thumbUrl(rec), alt: "", draggable: false,
                               loading: "lazy"});
        tile.appendChild(image);
        tile.appendChild(el("span", {className: "arrange-number", text: String(index + 1),
                                     attrs: {"aria-hidden": "true"}}));
        if (cover) {
            tile.appendChild(el("span", {className: "arrange-cover", text: "Cover",
                                         attrs: {"aria-hidden": "true"}}));
        }
        var tools = el("span", {className: "arrange-tools"});
        tools.appendChild(tileButton("\u2190", "Move earlier (Alt+\u2190)", function () {
            moveBy(index, -1);
        }));
        tools.appendChild(tileButton("\u2192", "Move later (Alt+\u2192)", function () {
            moveBy(index, 1);
        }));
        if (!cover || album.cover === null) {
            tools.appendChild(tileButton("\u2605", "Make it the cover (C)", function () {
                makeCover(id);
            }));
        }
        var remove = tileButton("\u00d7", "Take it out of the album (Delete)", function () {
            takeOut(id);
        });
        remove.classList.add("arrange-remove");
        tools.appendChild(remove);
        tile.appendChild(tools);
        tile.addEventListener("focus", function () {
            arranging.focus = index;
        });
        tile.addEventListener("pointerdown", function (event) {
            if (event.button !== 0 || event.pointerType === "touch" ||
                    event.target.closest(".arrange-button")) {
                return;
            }
            dragging = {id: id, startX: event.clientX, startY: event.clientY, active: false,
                        target: null, after: false, tile: tile};
        });
        return tile;
    }

    function dropMark(target, after) {
        Array.prototype.forEach.call(document.querySelectorAll(".arrange-tile.drop-before, " +
                                                               ".arrange-tile.drop-after"),
                                     function (node) {
            node.classList.remove("drop-before", "drop-after");
        });
        if (target) {
            target.classList.add(after ? "drop-after" : "drop-before");
        }
    }

    function setupDragging() {
        document.addEventListener("pointermove", function (event) {
            if (!dragging) {
                return;
            }
            if (!dragging.active) {
                if (Math.abs(event.clientX - dragging.startX) + Math.abs(event.clientY -
                                                                       dragging.startY) < 8) {
                    return;
                }
                dragging.active = true;
                dragging.tile.classList.add("is-dragging");
                document.body.classList.add("is-arranging");
            }
            event.preventDefault();
            var under = document.elementFromPoint(event.clientX, event.clientY);
            var target = under && under.closest ? under.closest(".arrange-tile") : null;
            if (target && target !== dragging.tile) {
                var box = target.getBoundingClientRect();
                dragging.target = target;
                dragging.after = event.clientX > box.left + box.width / 2;
                dropMark(target, dragging.after);
            } else {
                dragging.target = null;
                dropMark(null);
            }
        });
        function finish() {
            if (!dragging) {
                return;
            }
            var drag = dragging;
            dragging = null;
            dropMark(null);
            document.body.classList.remove("is-arranging");
            drag.tile.classList.remove("is-dragging");
            if (!drag.active || !drag.target) {
                return;
            }
            var targetId = idOf(drag.target.getAttribute("data-id"));
            var order = arranging.order.filter(function (other) {
                return other !== drag.id;
            });
            var at = order.indexOf(targetId) + (drag.after ? 1 : 0);
            move([drag.id], at >= order.length ? null : order[at]);
        }
        document.addEventListener("pointerup", finish);
        document.addEventListener("pointercancel", function () {
            if (dragging) {
                dragging.target = null;
            }
            finish();
        });
    }

    function idOf(text) {
        return /^\d+$/.test(text) ? Number(text) : text;
    }

    function arrangeKeydown(event) {
        var tile = event.target.closest ? event.target.closest(".arrange-tile") : null;
        if (!tile || event.target !== tile) {
            return;
        }
        var grid = tile.parentNode;
        var index = arranging.order.indexOf(idOf(tile.getAttribute("data-id")));
        var cols = columns(grid);
        var steps = {ArrowLeft: -1, ArrowRight: 1, ArrowUp: -cols, ArrowDown: cols};
        if (event.altKey && steps[event.key] !== undefined) {
            event.preventDefault();
            moveBy(index, steps[event.key]);
        } else if (event.altKey && (event.key === "Home" || event.key === "End")) {
            event.preventDefault();
            moveBy(index, event.key === "Home" ? -index : arranging.order.length);
        } else if (!event.altKey && steps[event.key] !== undefined) {
            event.preventDefault();
            focusTile(index + steps[event.key]);
        } else if (!event.altKey && (event.key === "Home" || event.key === "End")) {
            event.preventDefault();
            focusTile(event.key === "Home" ? 0 : arranging.order.length - 1);
        } else if (event.key === "Delete") {
            event.preventDefault();
            takeOut(arranging.order[index]);
        } else if (!event.altKey && !event.ctrlKey && !event.metaKey &&
                   (event.key === "c" || event.key === "C")) {
            event.preventDefault();
            makeCover(arranging.order[index]);
        }
    }

    function focusTile(index) {
        var tiles = document.querySelectorAll(".arrange-tile");
        index = Math.max(0, Math.min(tiles.length - 1, index));
        if (!tiles[index]) {
            return;
        }
        Array.prototype.forEach.call(tiles, function (node, at) {
            node.tabIndex = at === index ? 0 : -1;
        });
        arranging.focus = index;
        tiles[index].focus();
    }

    function drawArrange(keepFocus) {
        var album = arranging.album;
        byId("albums-title").textContent = album.name;
        byId("albums-lead").textContent = "Drag the pictures into the order you want, or use " +
            "the buttons on a picture. From the keyboard: the arrow keys go from picture to " +
            "picture, Alt+arrow keys move it (Alt+Home and Alt+End: to the start and the end), " +
            "C makes it the cover and Delete takes it out of the album. The search page and an " +
            "export show the album in this order.";
        var actions = byId("albums-actions");
        clear(actions);
        actions.appendChild(el("a", {className: "button", href: "/albums", text: "All albums",
                                     on: {click: function (event) {
                                         if (event.button === 0 && !event.ctrlKey &&
                                                 !event.metaKey && !event.shiftKey) {
                                             event.preventDefault();
                                             go(null, true);
                                         }
                                     }}}));
        actions.appendChild(el("a", {className: "button primary", href: searchUrl(album),
                                     text: "Open in search"}));
        var body = byId("albums-body");
        clear(body);
        statusBox = el("p", {className: "arrange-status", attrs: {"aria-live": "polite"},
                             text: fmt.plural(arranging.order.length, "picture")});
        body.appendChild(statusBox);
        if (!arranging.order.length) {
            body.appendChild(el("div", {className: "card album-none"}, [
                el("p", {text: "This album is empty. Put pictures in it from the search page."})
            ]));
            return;
        }
        var grid = el("ol", {className: "arrange-grid", attrs: {"aria-label": "The pictures of " +
                                                              album.name + ", in order"}});
        arranging.order.forEach(function (id, index) {
            grid.appendChild(arrangeTile(id, index));
        });
        grid.addEventListener("keydown", arrangeKeydown);
        body.appendChild(grid);
        if (keepFocus) {
            focusTile(arranging.focus);
        }
    }

    /* ---------- Pages ----------------------------------------------------------- */

    function render() {
        if (!data) {
            return;
        }
        if (arranging) {
            var album = albumById(arranging.album.id);
            if (!album) {
                arranging = null;
                notice("warn", "That album no longer exists.");
            } else {
                arranging.album = album;
                return;                         // (drawn by drawArrange)
            }
        }
        renderList();
    }

    function go(albumId, push) {
        var album = albumId !== null ? albumById(albumId) : null;
        if (albumId !== null && (!album || album.kind !== "manual" || !adminOn)) {
            notice("warn", album ? "Only the admin arranges a manual album." :
                "That album no longer exists.");
            album = null;
        } else {
            notice("", "");
        }
        if (push) {
            window.history.pushState(null, "", album ? "/albums?album=" +
                                     encodeURIComponent(album.id) : "/albums");
        }
        if (album) {
            arranging = {album: album, order: arrangeOrder(album), focus: 0};
            document.title = album.name + " - Albums - " + (SITE.title || "Image Search");
            drawArrange(false);
        } else {
            arranging = null;
            document.title = "Albums - " + (SITE.title || "Image Search");
            renderList();
        }
        window.scrollTo(0, 0);
    }

    function wanted() {
        var value = new URLSearchParams(window.location.search).get("album");
        return value && /^\d+$/.test(value) ? Number(value) : null;
    }

    function setData(loaded) {
        var lookup = {};
        loaded.records.forEach(function (rec) {
            lookup[rec.i] = rec;
        });
        data = {records: loaded.records, byId: lookup,
                albums: Array.isArray(loaded.albums) ? loaded.albums : [],
                index: SearchCore.prepare(loaded.records)};
        if (vectors) {
            data.index.setVectors(vectors);
        }
        if (looksAsked) {
            SITE.attachLooks(data.index);
        }
    }

    // After a change: the index again (counts, covers and names come from it).
    function reload() {
        SITE.refreshIndex(function (loaded) {
            if (loaded) {
                setData(loaded);
                wantPeople();
            }
            go(arranging ? arranging.album.id : null, false);
        });
    }

    function start() {
        setupDragging();
        window.addEventListener("popstate", function () {
            go(wanted(), false);
        });
        SITE.loadIndex(function (loaded) {
            if (!loaded) {
                clear(byId("albums-body"));
                byId("albums-body").appendChild(SHELL.notice("error",
                    "The albums could not be loaded. Is the server still running?"));
                return;
            }
            setData(loaded);
            wantPeople();
            go(wanted(), false);
            wantLooks();
            // Smart albums that search by meaning: their counts once the
            // vectors are there.
            if (data.albums.some(function (album) {
                return album.kind === "smart";
            })) {
                SITE.loadVectors(function (loadedVectors) {
                    vectors = loadedVectors || null;
                    if (vectors && data) {
                        data.index.setVectors(vectors);
                        if (!arranging) {
                            renderList();
                        }
                    }
                });
            }
        });
    }

    start();
})();
