/* EXPORT_REVIEW: the words and rules of the Export dialog (Manage page),
   without the page.

   The dialog (manage.js) lets the owner choose what an export publishes
   (export.py: DEFAULT_OPTIONS) and then shows the pictures the AI model
   flagged (documents, screenshots, private, sensitive, not described yet)
   so each can be published or left out. This file holds what the options
   mean in plain words, which flags leave a picture out under the options,
   the choices made picture by picture (include_ids / exclude_ids), and the
   summaries the dialog shows. tests/js/export_review_test.js runs it under
   Node.

     EXPORT_REVIEW.OPTIONS             option -> {label, hint} (images: per mode)
     EXPORT_REVIEW.TYPES               picture types the dialog offers to leave out
     EXPORT_REVIEW.activeReasons(item, options)   the flags that leave it out
     EXPORT_REVIEW.published(item, options)       true when it is published
     EXPORT_REVIEW.newFlags(item, options)  flags it got since it was chosen
                                       to be published (they leave it out)
     EXPORT_REVIEW.choose(options, item, publish) new options with that choice
     EXPORT_REVIEW.recount(reply, options)  the preview's counts and size
                                       after choices made in the review
     EXPORT_REVIEW.groups(flagged)     [{key, title, hint, items}] for the grid
     EXPORT_REVIEW.reasonWords(reasons)      "screenshot, private information"
     EXPORT_REVIEW.leftOut(counts)     ["1,204 pictures not described yet", ...]
     EXPORT_REVIEW.headline(counts)    "312 of 1,699 pictures will be published."
     EXPORT_REVIEW.sizeText(preview)   "About 162 MB: thumbnails 12 MB, ..."
     EXPORT_REVIEW.similarText(counts, options)  what "More like this" will
                                       have to go by, or ""
     EXPORT_REVIEW.meaningText(counts, options)  what search by meaning in
                                       the browser costs, or why it cannot
                                       go, or ""
     EXPORT_REVIEW.looksText(counts, options, parts)  how many pictures
                                       "Looks like this" works for and what
                                       it adds, or ""
     EXPORT_REVIEW.streetsText(counts, options)  what goes of the street maps
                                       (counts.street_maps), or ""
     EXPORT_REVIEW.worldText(counts, options)  what goes of the world map's
                                       tiles (counts.world_map), or ""
     EXPORT_REVIEW.detailsText(options)  what is published with each picture
     EXPORT_REVIEW.serveCommand(folder)  the command that tries a site locally
     EXPORT_REVIEW.bytes(n)            "162 MB"

   An item is one entry of the flagged list of /export/preview: {id,
   reason, reasons, title, excluded, bytes, parts, new_flags, video (a
   video: "included", "not_playable", "too_large" or "poster")}. A picture
   published despite its flags is [id, [those flags]] in include_ids, and
   is left out again when it gets another flag that leaves it out (as
   export.py decides).
*/
var EXPORT_REVIEW = (function () {
    "use strict";

    var IMAGE_MODES = {
        web: {label: "Web size (recommended)",
              hint: "Pictures up to 2048 pixels wide, re-saved without the details cameras " +
                    "and phones hide in the files (GPS position, time, serial numbers). " +
                    "About 0.5 MB each."},
        original: {label: "Originals",
                   hint: "The original files, with the details hidden in them (GPS " +
                         "position, time, serial numbers, software) taken out; HEIC, AVIF " +
                         "and TIFF become JPEG. Full quality, but much larger."},
        none: {label: "Thumbnails only",
               hint: "Small previews only: the smallest site, for searching rather than " +
                     "looking."}
    };

    // Every option of export.py's DEFAULT_OPTIONS but the id lists.
    var OPTIONS = {
        images: IMAGE_MODES,
        exclude_types: {label: "Kinds of picture",
                        hint: "Kinds of picture that are left out."},
        exclude_private: {label: "Pictures with private information",
                          hint: "ID and bank cards, account numbers, passwords, addresses, " +
                                "medical or money matters, private messages."},
        exclude_sensitive: {label: "Sensitive pictures",
                            hint: "Suggestive, explicit or graphic pictures."},
        exclude_undescribed: {label: "Pictures not checked yet",
                              hint: "Not described yet, or described by an older version " +
                                    "that did not look for private or sensitive content: " +
                                    "none of the checks here could be made."},
        file_names: {label: "File names",
                     hint: "Shown and searchable. They often hold dates, names or places."},
        folders: {label: "Folder names",
                  hint: "Shown and searchable, such as Holidays/2019 Rome."},
        location: {label: "Location",
                   hint: "GPS positions (rounded to about 10 m), the town, region and " +
                         "country of each (when place names were downloaded, with a " +
                         "GeoNames credit at the foot of the pages), the named place on " +
                         "the street maps where each was taken (a park, a museum, a cafe, " +
                         "a hotel, with an OpenStreetMap credit), the map, the areas of " +
                         "the map of smart albums, and the AI model's separate landmark " +
                         "and place-guess fields. Shows where pictures were taken, which " +
                         "may include your home. Titles and descriptions can still name a " +
                         "place a picture shows; read them in the review."},
        estimated_places: {label: "Estimated places",
                           hint: "With Location: the places estimated for pictures without " +
                                 "a GPS position of their own (from a copy, the pictures " +
                                 "taken around the same time or the library's area), marked " +
                                 "as estimated, on the map as hollow pins. They can be wrong. " +
                                 "Places you set yourself go with Location."},
        routes_trim: {label: "Left out where drives start and end",
                      hint: "Metres of every drive's route left out around where it " +
                            "starts and where it ends (0 to 5,000; 500 by default)."},
        routes: {label: "Drive routes",
                 hint: "With Location: the routes of drives (the GPS tracks dashcams record " +
                       "into their videos) on the map and in the viewer, and the positions " +
                       "of their clips. The first and last 500 m of every drive (the " +
                       "routes_trim option) are left out, so where drives start and end " +
                       "(often your home) is not shown. Without it, clips keep their place " +
                       "names but not their positions."},
        street_maps: {label: "Street maps",
                      hint: "With Location: the street maps downloaded on the Manage page, " +
                            "only around the published places and along the published " +
                            "routes, so the site's maps show streets offline too " +
                            "(OpenStreetMap data, credited at the foot of the pages). They " +
                            "show where the pictures are, as Location does; the review says " +
                            "how large they are."},
        street_km: {label: "Around each place",
                    hint: "Kilometres of street map around each published place (and 2 km " +
                          "either side of the routes)."},
        street_zoom: {label: "Street map detail",
                      hint: "The most detailed street maps that go along: zoom 15 shows " +
                            "every building, 14 the streets, 12 the main roads; each step " +
                            "down makes them about three times smaller."},
        world_zoom: {label: "World map detail",
                     hint: "With Location: the most detailed zoom of the world map's " +
                           "tiles that go along; the maps draw closer views from them, " +
                           "a little blurred, until the street maps take over. Each zoom " +
                           "has four times the tiles of the one before (zoom 6 alone is " +
                           "about twice as large as zoom 0 to 5): the review says how large " +
                           "they are."},
        title: {label: "Site title",
                hint: "Shown in the header and the browser tab."},
        credits: {label: "Footer line",
                  hint: "A line of plain text at the foot of every page, such as " +
                        "\u00a9 2026 Your Name."},
        zip: {label: "Also make a zip file",
              hint: "<folder>.zip next to the folder, ready to upload or send."},
        allow_indexing: {label: "Let search engines index the site",
                         hint: "Off: the pages ask search engines (Google, Bing, ...) not " +
                               "to list them or their pictures. Either way, anyone who has " +
                               "the address can open the site and every picture in it."},
        videos: {label: "Include videos",
                 hint: "Videos browsers play (H.264, or a playable copy) up to {mb} MB each, " +
                       "copied without the details hidden in them (place, time, device). " +
                       "Off, or for other videos (HEVC): the poster frame only. The AI " +
                       "model saw only a few still frames of each video and never heard " +
                       "its sound: watch the videos you publish."},
        raw_files: {label: "RAW files",
                    hint: "The camera RAW file of each picture that has one, as the " +
                          "camera wrote it, for visitors to download: with every fact " +
                          "in it (the time to the second, the camera and its serial " +
                          "number). One that holds a GPS position goes only with " +
                          "Location. They are large."},
        max_video_mb: {label: "Largest video",
                       hint: "Videos larger than this many megabytes are published as their " +
                             "poster frame only (the review step counts them)."},
        similar: {label: "More like this by meaning",
                  hint: "Each picture's meaning vector (about 350 bytes), made from its " +
                        "description, so \u201cMore like this\u201d finds pictures that mean " +
                        "much the same. It holds nothing the description does not show."},
        meaning: {label: "Search by meaning in the browser",
                  hint: "The small embedding model goes along, with its license, so what " +
                        "visitors type is found by meaning too, offline and from disk. " +
                        "Visitors download it once, the first time they search (the " +
                        "review says how much); choosing Words never loads it."},
        looks: {label: "Looks like this",
                hint: "Each picture's look vector (about 350 bytes), made by the image " +
                      "model from the picture itself, so \u201cLooks like this\u201d finds " +
                      "pictures that look alike. Searching by an example picture needs " +
                      "the server's image model: published sites do not have it."},
        home_km: {label: "Keep home private",
                  hint: "No GPS position, drive route or town within about this many " +
                        "kilometres of home (squares of a grid around it, so that its edge " +
                        "does not point at home), and no dashcam clip filmed there or " +
                        "without a GPS fix, nor the words of its drive (the review lists " +
                        "them). Keep to one size for all your trips: sites made with " +
                        "different sizes together tell no more than the smallest."},
        album: {label: "The pictures of",
                hint: "Choose what to publish: put the pictures in an album (a manual " +
                      "one) on the search page, then pick it here. The checks below still " +
                      "apply to them."},
        story: {label: "The trip's story",
                hint: "With Location (it names the trip's places): the story written for " +
                      "the trip on its page, a paragraph a day. It names people only with " +
                      "People's names on (and only those in the pictures published), and " +
                      "leaves out the sentences about pictures that are not published or " +
                      "near home, and those naming a town of home."},
        people_names: {label: "People's names (as tags)",
                       hint: "The names you gave people on the People page, as tags of the " +
                             "pictures they are in, so a search for a name finds them in the " +
                             "site. Unnamed and hidden people are left out. Faces, their " +
                             "boxes and what was learnt from them are never published."},
        marks: {label: "Favorites, ratings and albums",
                hint: "Which pictures are your favorites, their star ratings, and the " +
                      "names of your albums (with the searches of smart albums), so " +
                      "is:favorite, rating: and album: work in the site. Off: none of it " +
                      "is published, not even the name of the album chosen above."}
    };

    // The picture types the dialog offers to leave out (others stay as saved).
    var TYPES = {
        screenshot: {label: "Screenshots",
                     hint: "Screens of phones and computers, also photographed: messages, " +
                           "accounts, apps."},
        document: {label: "Documents",
                   hint: "Letters, receipts, forms, tickets and other papers, also " +
                         "photographed."},
        scan: {label: "Scans", hint: "Scanned paper and photos of pages."},
        video: {label: "Videos", hint: "Every video, its poster frame too."}
    };

    // Groups of the review grid, in order, and the words for each reason.
    var REASONS = {
        "private": {title: "Private information", one: "picture with private information",
                    many: "pictures with private information", word: "private information",
                    hint: "The AI model saw ID or bank cards, account numbers, passwords, " +
                          "addresses, medical or money matters, or private messages."},
        sensitive: {title: "Sensitive", one: "sensitive picture", many: "sensitive pictures",
                    word: "sensitive",
                    hint: "Suggestive, explicit or graphic, according to the AI model."},
        home: {title: "Near home", one: "dashcam clip that may show where home is",
               many: "dashcam clips that may show where home is", word: "near home",
               hint: "Dashcam clips filmed near home, or without a GPS fix: a dashcam " +
                     "prints its position over its video (without a fix, the last one it " +
                     "had, often where the drive started). Watch one before you publish it."},
        document: {title: "Documents", one: "document", many: "documents", word: "document",
                   hint: "Letters, receipts, forms, tickets and other papers."},
        screenshot: {title: "Screenshots", one: "screenshot", many: "screenshots",
                     word: "screenshot",
                     hint: "Screens of phones and computers: messages, accounts, apps."},
        scan: {title: "Scans", one: "scan", many: "scans", word: "scan",
               hint: "Scanned paper. Check them for names, addresses and numbers."},
        writing: {title: "Other pictures with writing", one: "picture with writing",
                  many: "pictures with writing", word: "writing",
                  hint: "Signs, menus, book pages, whiteboards, slides, maps: published " +
                        "unless you untick them. Check them for names, addresses and numbers."},
        artwork: {title: "Artwork", one: "artwork", many: "artworks", word: "artwork"},
        meme: {title: "Memes", one: "meme", many: "memes", word: "meme"},
        diagram: {title: "Diagrams", one: "diagram", many: "diagrams", word: "diagram"},
        other: {title: "Other pictures", one: "other picture", many: "other pictures",
                word: "other"},
        video: {title: "Videos", one: "video", many: "videos", word: "video",
                hint: "Left out with every other video (Videos under Leave out)."},
        photo: {title: "Photos", one: "photo", many: "photos", word: "photo"},
        unchecked: {title: "Described by an older version",
                    one: "picture described by an older version (not checked)",
                    many: "pictures described by an older version (not checked)",
                    word: "older description, not checked",
                    hint: "An older version of this program described these without " +
                          "looking for private or sensitive content or the kind of picture. " +
                          "Describe them again (Re-describe stale on the library) to check " +
                          "them."},
        undescribed: {title: "Not described yet", one: "picture not described yet",
                      many: "pictures not described yet", word: "not described yet",
                      hint: "The AI model has not looked at these yet, so they were not " +
                            "checked for anything above."},
        hidden: {one: "hidden picture (never published)",
                 many: "hidden pictures (never published)"},
        excluded: {one: "picture you left out", many: "pictures you left out"},
        chosen: {title: "Chosen by you",
                 hint: "Pictures you published or left out one by one."}
    };
    var GROUP_ORDER = ["private", "sensitive", "home", "document", "screenshot", "scan",
                       "writing", "artwork", "meme", "diagram", "other", "video", "photo",
                       "unchecked", "undescribed"];
    var LEFT_OUT_ORDER = GROUP_ORDER.concat(["excluded", "hidden"]);

    function number(value) {
        var n = Number(value);
        return isFinite(n) ? n.toLocaleString("en-US") : "0";
    }

    function bytes(value) {
        var n = Number(value);
        if (!isFinite(n) || n < 0) {
            return "";
        }
        var units = ["bytes", "KB", "MB", "GB", "TB"];
        var unit = 0;
        while (n >= 1024 && unit < units.length - 1) {
            n /= 1024;
            unit += 1;
        }
        return (unit === 0 ? String(Math.round(n)) : n.toFixed(n < 10 ? 1 : 0)) + " " +
            units[unit];
    }

    function has(list, id) {
        return Array.isArray(list) && list.indexOf(id) >= 0;
    }

    // The id of an include_ids entry: an id, or [id, flags].
    function entryId(entry) {
        return Array.isArray(entry) ? entry[0] : entry;
    }

    function without(list, id) {
        return (Array.isArray(list) ? list : []).filter(function (item) {
            return entryId(item) !== id;
        });
    }

    function sorted(list) {
        return list.slice().sort(function (a, b) {
            return entryId(a) - entryId(b);
        });
    }

    // The include_ids entry of a picture: null, or {flags: null (whatever
    // its flags) or [the flags it is published despite]}.
    function included(options, id) {
        var list = Array.isArray(options.include_ids) ? options.include_ids : [];
        for (var k = 0; k < list.length; k++) {
            if (entryId(list[k]) === id) {
                return {flags: Array.isArray(list[k]) && Array.isArray(list[k][1])
                    ? list[k][1] : null};
            }
        }
        return null;
    }

    // The flags of item that leave it out under options (whatever its id lists say).
    function activeReasons(item, options) {
        var types = options.exclude_types || [];
        return (item.reasons || []).filter(function (reason) {
            if (reason === "undescribed" || reason === "unchecked") {
                return Boolean(options.exclude_undescribed);
            }
            if (reason === "private") {
                return Boolean(options.exclude_private);
            }
            if (reason === "sensitive") {
                return Boolean(options.exclude_sensitive);
            }
            if (reason === "home") {
                // (A trip's export flags them only while it keeps home private.)
                return true;
            }
            return types.indexOf(reason) >= 0;
        });
    }

    // The flags that leave item out although it was chosen to be published
    // (it got them since), or [] (export.py: select_records).
    function newFlags(item, options) {
        var entry = included(options, item.id);
        if (!entry || entry.flags === null || has(options.exclude_ids, item.id)) {
            return [];
        }
        return activeReasons(item, options).filter(function (reason) {
            return entry.flags.indexOf(reason) < 0;
        });
    }

    function published(item, options) {
        if (has(options.exclude_ids, item.id)) {
            return false;
        }
        var active = activeReasons(item, options);
        if (!active.length) {
            return true;
        }
        return included(options, item.id) !== null && !newFlags(item, options).length;
    }

    // options (a new object) with item published or left out. A picture
    // only goes on a list when the rules would decide otherwise; one
    // published despite its flags is kept with them ([id, flags]), so a
    // flag it gets later leaves it out again.
    function choose(options, item, publish) {
        var result = {};
        Object.keys(options).forEach(function (key) {
            result[key] = options[key];
        });
        var include = without(options.include_ids, item.id);
        var exclude = without(options.exclude_ids, item.id);
        var byRules = activeReasons(item, options).length > 0;
        if (publish && byRules) {
            include.push([item.id, (item.reasons || []).slice()]);
        }
        if (!publish && !byRules) {
            exclude.push(item.id);
        }
        result.include_ids = sorted(include);
        result.exclude_ids = sorted(exclude);
        return result;
    }

    // Why the preview counts a picture as left out under options: "excluded"
    // (left out by choice), its first flag that leaves it out, or null.
    function countedAs(item, options) {
        if (has(options.exclude_ids, item.id)) {
            return "excluded";
        }
        if (published(item, options)) {
            return null;
        }
        return activeReasons(item, options)[0] || null;
    }

    // The counts and size of a preview reply (made with reply.options)
    // after choices made since in the review (options: the same but for
    // include_ids and exclude_ids), worked out here so that a click needs
    // no new answer from the server.
    function recount(reply, options) {
        var before = reply.options || options;
        var counts = JSON.parse(JSON.stringify(reply.counts || {}));
        var reasons = counts.by_reason = counts.by_reason || {};
        var parts = JSON.parse(JSON.stringify(reply.size_parts || {}));
        var size = typeof reply.size_estimate === "number" ? reply.size_estimate : null;
        (reply.flagged || []).forEach(function (item) {
            var was = countedAs(item, before);
            var now = countedAs(item, options);
            if (was === now) {
                return;
            }
            // +1: published now (it was left out); -1: left out now.
            var delta = was === null ? -1 : now === null ? 1 : 0;
            if (was !== null) {
                reasons[was] = (reasons[was] || 0) - 1;
                if (reasons[was] <= 0) {
                    delete reasons[was];
                }
            }
            if (now !== null) {
                reasons[now] = (reasons[now] || 0) + 1;
            }
            if (delta) {
                counts.exported = (counts.exported || 0) + delta;
                if (size !== null) {
                    size += delta * (item.bytes || 0);
                }
                Object.keys(item.parts || {}).forEach(function (key) {
                    parts[key] = (parts[key] || 0) + delta * item.parts[key];
                });
                if (counts.similar) {
                    var kind = item.parts && item.parts.vectors ? "vectors" : "missing";
                    counts.similar[kind] = Math.max(0, (counts.similar[kind] || 0) + delta);
                }
                if (counts.looks) {
                    var lookKind = item.parts && item.parts.looks ? "vectors" : "missing";
                    counts.looks[lookKind] = Math.max(0, (counts.looks[lookKind] || 0) + delta);
                }
                if (item.raw && counts.raws) {
                    counts.raws.total = Math.max(0, (counts.raws.total || 0) + delta);
                    counts.raws[item.raw] = Math.max(0, (counts.raws[item.raw] || 0) + delta);
                }
                if (item.video && counts.videos) {
                    var videos = counts.videos;
                    videos.total = Math.max(0, (videos.total || 0) + delta);
                    // (A poster only, as videos are not included: no key.)
                    if (item.video !== "poster") {
                        videos[item.video] = Math.max(0, (videos[item.video] || 0) + delta);
                    }
                }
                if (item.needs_folder && typeof counts.needs_folder === "number") {
                    counts.needs_folder += delta;
                    if (counts.unavailable) {
                        counts.unavailable += delta;
                    }
                }
            }
        });
        var result = {};
        Object.keys(reply).forEach(function (key) {
            result[key] = reply[key];
        });
        result.counts = counts;
        result.size_parts = parts;
        result.size_estimate = size;
        return result;
    }

    function groupKey(item) {
        var reasons = item.reasons || [];
        for (var k = 0; k < GROUP_ORDER.length; k++) {
            if (reasons.indexOf(GROUP_ORDER[k]) >= 0) {
                return GROUP_ORDER[k];
            }
        }
        return "chosen";
    }

    function groups(flagged) {
        var byKey = {};
        (flagged || []).forEach(function (item) {
            var key = groupKey(item);
            (byKey[key] = byKey[key] || []).push(item);
        });
        return GROUP_ORDER.concat(["chosen"]).filter(function (key) {
            return byKey[key];
        }).map(function (key) {
            var words = REASONS[key] || {};
            return {key: key, title: words.title || key, hint: words.hint || "",
                    items: byKey[key]};
        });
    }

    function reasonWords(reasons) {
        return (reasons || []).map(function (reason) {
            return (REASONS[reason] && REASONS[reason].word) || reason;
        }).join(", ");
    }

    function counted(count, key) {
        var words = REASONS[key] || {};
        return number(count) + " " + (Number(count) === 1 ? words.one || key
                                                           : words.many || key);
    }

    // What is left out, biggest reasons first in the dialog's order.
    function leftOut(counts) {
        var reasons = (counts && counts.by_reason) || {};
        var keys = LEFT_OUT_ORDER.filter(function (key) {
            return reasons[key];
        }).concat(Object.keys(reasons).filter(function (key) {
            return reasons[key] && LEFT_OUT_ORDER.indexOf(key) < 0;
        }).sort());
        return keys.map(function (key) {
            return counted(reasons[key], key);
        });
    }

    function headline(counts) {
        counts = counts || {};
        var total = Number(counts.total) || 0;
        var exported = Number(counts.exported) || 0;
        if (!total) {
            return counts.album ? "The album has no pictures in this library."
                : counts.trip ? "The trip has no pictures to publish."
                : "This library has no pictures to publish yet.";
        }
        if (!exported) {
            return "No picture would be published: every one is left out.";
        }
        if (counts.album && counts.album.name) {
            return number(exported) + " of the " + number(total) + (total === 1 ? " picture"
                : " pictures") + " of the album \u201c" + counts.album.name + "\u201d will be " +
                "published.";
        }
        if (counts.trip && counts.trip.title) {
            return number(exported) + " of the " + number(total) + (total === 1 ? " picture"
                : " pictures") + " of the trip \u201c" + counts.trip.title + "\u201d will be " +
                "published.";
        }
        return number(exported) + " of " + number(total) + (total === 1 ? " picture" :
            " pictures") + " will be published.";
    }

    function sizeText(preview) {
        if (!preview || typeof preview.size_estimate !== "number") {
            return "";
        }
        var parts = preview.size_parts || {};
        var named = [["thumbnails", parts.thumbs], ["pictures", parts.images],
                     ["large panoramas", parts.panos], ["videos", parts.videos],
                     ["RAW files", parts.raws],
                     ["pages and data", parts.data], ["meaning vectors", parts.vectors],
                     ["look vectors", parts.looks], ["search by meaning", parts.meaning],
                     ["street maps", parts.maps], ["world map", parts.tiles]];
        var list = named.filter(function (item) {
            return item[1] > 0;
        }).map(function (item) {
            return item[0] + " " + bytes(item[1]);
        });
        return "About " + bytes(preview.size_estimate) + (list.length > 1 ? ": " +
            list.join(", ") : "") + ".";
    }

    /* What happens to the videos among the pictures to publish (counts:
       preview_export's counts.videos {total, included, not_playable,
       too_large}), in a sentence; "" when there are none. */
    function videoText(counts, options) {
        if (!counts || !counts.total) {
            return "";
        }
        var total = Number(counts.total) || 0;
        var some = total === 1 ? "The video" : "The " + number(total) + " videos";
        if (!options || options.videos !== "include") {
            return some + " to publish " + (total === 1 ? "is" : "are") + " published as " +
                (total === 1 ? "its poster frame" : "their poster frames") +
                " (Include videos publishes the videos themselves).";
        }
        var parts = [number(counts.included || 0) + " of " + number(total) +
                     (total === 1 ? " video is" : " videos are") + " published as " +
                     ((counts.included || 0) === 1 ? "a video" : "videos")];
        if (counts.not_playable) {
            parts.push(number(counts.not_playable) + " only as a poster frame: most browsers " +
                       "cannot play " + (counts.not_playable === 1 ? "it" : "them") +
                       " (HEVC), and there is no playable copy");
        }
        if (counts.too_large) {
            parts.push(number(counts.too_large) + " only as a poster frame: larger than " +
                       number(options.max_video_mb) + " MB");
        }
        return parts.join("; ") + "." + (counts.included ? " The AI model saw only a few " +
            "still frames of each video and never heard its sound: watch the videos you " +
            "publish." : "");
    }

    /* What happens to the camera RAW files of the pictures to publish (counts:
       preview_export's counts.raws {total, included, location}), in a
       sentence; "" when none has one. */
    function rawsText(counts, options) {
        if (!counts || !counts.total) {
            return "";
        }
        var total = Number(counts.total) || 0;
        if (!options || !options.raw_files) {
            return number(total) + (total === 1 ? " picture has a RAW file; it stays home"
                : " pictures have RAW files; they stay home") + " (RAW files publishes them).";
        }
        var text = number(counts.included || 0) + " of " + number(total) +
            (total === 1 ? " RAW file goes" : " RAW files go") + " along as the camera " +
            "wrote " + (total === 1 ? "it" : "them") + ", with every fact in " +
            (total === 1 ? "it" : "them") + ": the time to the second, the camera and its " +
            "serial number";
        if (counts.location) {
            text += "; " + number(counts.location) + " stay" + (counts.location === 1 ? "s" : "") +
                " home: " + (counts.location === 1 ? "it holds" : "they hold") + " a GPS " +
                "position, and Location is off or " + (counts.location === 1 ? "its picture's"
                : "their pictures'") + " own position is set aside" +
                // (A trip's export: one near home stays too, see export.raw_plan.)
                (typeof options.home_km === "number" && options.home_km > 0 ? " or near home"
                    : "");
        }
        return text + ".";
    }

    /* The drives among the pictures to publish (counts.drives of the preview:
       {drives, described, private, sensitive, withheld}, see
       drives.export_texts and export.withheld_clips): how many keep their own
       description, and how many lose it to the options or to a clip left out
       near home (a trip's export); "" when none has one. */
    function drivesText(counts) {
        var drives = counts && counts.drives;
        var left = drives ? (drives["private"] || 0) + (drives.sensitive || 0) +
            (drives.withheld || 0) : 0;
        if (!drives || !drives.drives || (!drives.described && !left)) {
            return "";
        }
        var parts = [number(drives.described || 0) + " of " + number(drives.drives) +
                     (drives.drives === 1 ? " drive is" : " drives are") +
                     " published with " + (drives.drives === 1 ? "its" : "their") +
                     " own description"];
        if (drives["private"]) {
            parts.push(number(drives["private"]) + " without it (private information)");
        }
        if (drives.sensitive) {
            parts.push(number(drives.sensitive) + " without it (sensitive)");
        }
        if (drives.withheld) {
            parts.push(number(drives.withheld) + " without it (written from clips filmed " +
                       "near home, which are left out)");
        }
        return parts.join("; ") + ".";
    }

    /* What goes out of the videos' GPS tracks (counts.routes of the preview:
       {on, positions, withheld, routes}, see export.py's _Routes): the routes
       and the clips' positions published or left out; "" when no picture to
       publish has a position from a track. */
    function routesText(counts) {
        var found = counts && counts.routes;
        if (!found || !found.positions) {
            return "";
        }
        if (!found.on) {
            return number(found.positions) + (found.positions === 1 ? " clip keeps its"
                : " clips keep their") + " place names but not the position from " +
                (found.positions === 1 ? "its" : "their") + " GPS track (Drive routes is off).";
        }
        var text = number(found.routes || 0) + (found.routes === 1 ? " drive route is"
            : " drive routes are") + " published";
        if (found.withheld) {
            text += "; " + number(found.withheld) + (found.withheld === 1 ? " clip" : " clips") +
                " near where a drive starts or ends keep only " +
                (found.withheld === 1 ? "its" : "their") + " place names";
        }
        return text + ".";
    }

    /* A warning about the dashcam clips to publish (counts.routes of the
       preview: {overlay, near}): their pictures show the position their
       overlay prints, whatever the options; "" when there are none. */
    function overlayText(counts) {
        var found = counts && counts.routes;
        if (!found || !found.overlay) {
            return "";
        }
        var one = found.overlay === 1;
        var text = number(found.overlay) + (one ? " dashcam clip is" : " dashcam clips are") +
            " published (as " + (one ? "a video or its poster frame" : "videos or poster frames") +
            "). A dashcam prints its GPS position over its video, so " +
            (one ? "its picture shows it" : "their pictures show it") +
            " whatever Location and Drive routes say (the positions in their texts are left " +
            "out).";
        if (found.near) {
            text += " " + number(found.near) + (found.near === 1 ? " of them was" :
                " of them were") + " filmed near where a drive starts or ends, or without a " +
                "GPS fix (the overlay then shows the last position the camera had, often " +
                "where the drive started): hide " + (found.near === 1 ? "it" : "them") +
                " to keep that place private.";
        }
        return text;
    }

    // "More like this" in the export: how many pictures it can go by meaning
    // for (counts.similar of the preview), or "" when the option is off.
    function similarText(counts, options) {
        var similar = counts && counts.similar;
        if (!options || !options.similar || !similar) {
            return "";
        }
        var without = (similar.stale || 0) + (similar.missing || 0);
        var exported = Number(counts.exported) || 0;
        if (!exported) {
            return "";
        }
        if (!without) {
            return "More like this goes by meaning for every published picture.";
        }
        if (!similar.vectors) {
            return "No published picture has a meaning vector yet, so More like this goes by " +
                "tags and dates (set up search by meaning and update it on the Manage page).";
        }
        return "More like this goes by meaning for " + number(similar.vectors) + " of " +
            number(exported) + (exported === 1 ? " picture" : " pictures") + "; " +
            number(without) + (without === 1 ? " has" : " have") + " no up-to-date meaning " +
            "vector (Update meaning search on the Manage page first).";
    }

    /* Search by meaning in the browser (counts.meaning of the preview:
       {available, reason, bytes, gzip}): its size and download when it
       goes, the reason when the option is on but it cannot go, else "". */
    function meaningText(counts, options) {
        var meaning = counts && counts.meaning;
        if (!meaning || !options || !options.meaning) {
            return "";
        }
        if (!meaning.available) {
            return meaning.reason ? "Search by meaning in the browser is not included: " +
                meaning.reason : "";
        }
        if (!Number(counts.exported)) {
            return "";
        }
        return "Search by meaning in the browser: " + bytes(meaning.bytes) + " of model " +
            "files; visitors download about " + bytes(meaning.gzip) + " once, the first " +
            "time they search (works offline and from disk).";
    }

    /* "Looks like this" in the export (counts.looks of the preview: {vectors,
       missing}; parts: its size_parts): for how many pictures, and its size;
       "" when the option is off or nothing would go. (A look vector makes the
       search possible; the viewer offers it only on a picture others look
       like: viewer.js renderInfo.) */
    function looksText(counts, options, parts) {
        var looked = counts && counts.looks;
        var exported = Number(counts && counts.exported) || 0;
        if (!options || !options.looks || !looked || !exported) {
            return "";
        }
        if (!looked.vectors) {
            return "No published picture has a look vector yet, so Looks like this is not " +
                "included (Update looks on the Manage page first).";
        }
        var bytesEach = parts && parts.looks ? Math.round(parts.looks / looked.vectors) : 0;
        return "Looks like this works for " + (looked.missing ? number(looked.vectors) +
            " of " + number(exported) + (exported === 1 ? " picture" : " pictures")
            : looked.vectors === 1 ? "the published picture" : "every published picture") +
            (bytesEach ? " (about " + number(bytesEach) + " bytes a picture, " +
             bytes(parts.looks) + " in all)" : "") + (looked.missing ? "; " +
            number(looked.missing) + (looked.missing === 1 ? " has" : " have") + " no look " +
            "vector yet (Update looks on the Manage page first)" : "") + ". It is offered " +
            "on a picture when other published pictures look like it.";
    }

    /* What goes of the street maps (counts.street_maps of the preview:
       {available, on, tiles, bytes}), in a sentence; "" when they are not
       asked for. */
    function streetsText(counts, options) {
        var found = counts && counts.street_maps;
        if (!found || !options || !options.location || !options.street_maps) {
            return "";
        }
        if (!found.available) {
            return "There are no street maps to publish: download them on the Manage page " +
                "first. The site's maps show the world image only.";
        }
        if (!found.tiles) {
            return "No street map goes along: none was downloaded around the published " +
                "places.";
        }
        return "Street maps: " + number(found.tiles) + (found.tiles === 1 ? " tile" : " tiles") +
            " (" + bytes(found.bytes) + "), " + number(options.street_km) + " km around the " +
            "published places, down to zoom " + number(options.street_zoom) +
            (options.home_km ? ", none near home" : "") + ".";
    }

    /* What goes of the world map's tiles (counts.world_map of the preview:
       {available (the highest zoom there is; null: the bundled small map),
       zoom, tiles, bytes, more: {zoom, tiles, bytes} of the next zoom}), in a
       sentence; "" without location or a tile pyramid. */
    function worldText(counts, options) {
        var found = counts && counts.world_map;
        if (!found || !options || !options.location || typeof found.available !== "number") {
            return "";
        }
        var text = "World map: " + number(found.tiles) + (found.tiles === 1 ? " tile" :
            " tiles") + " (" + bytes(found.bytes) + "), down to zoom " + number(found.zoom);
        if (found.more) {
            text += "; zoom " + number(found.more.zoom) + " would add " +
                number(found.more.tiles) + (found.more.tiles === 1 ? " tile" : " tiles") +
                " (" + bytes(found.more.bytes) + ")";
        }
        return text + ".";
    }

    // What each picture carries into the export, in a sentence or two.
    function detailsText(options) {
        var shown = ["title", "description", "tags", "any text read in the picture",
                     "the day it was taken (from the camera or the file name)",
                     "camera model and settings"];
        var hidden = [];
        (options.file_names ? shown : hidden).push("file name");
        (options.folders ? shown : hidden).push("folder");
        (options.location ? shown : hidden).push("location (GPS position, place names and " +
                                                 "named place)");
        if (options.location && options.estimated_places) {
            shown.push("estimated place");
        }
        if (options.location && options.routes) {
            shown.push("drive route (less where drives start and end)");
        }
        if (options.marks) {
            // (Only said when on: most libraries have no marks.)
            shown.push("favorite star, rating and albums");
        }
        if (options.people_names) {
            shown.push("the names of the people in it (as tags)");
        }
        if (options.raw_files) {
            shown.push("RAW file, when it has one (as the camera wrote it)");
        }
        var text = "Published with each picture: its " + shown.slice(0, -1).join(", ") +
            " and " + shown[shown.length - 1] + ".";
        if (hidden.length) {
            text += " Not its " + (hidden.length === 1 ? hidden[0] : hidden.slice(0, -1)
                .join(", ") + " or " + hidden[hidden.length - 1]) + ".";
        }
        if (options.location && typeof options.home_km === "number" && options.home_km > 0) {
            text += " Nothing within about " + number(options.home_km) + " km of home: no " +
                "position, route, town or named place there.";
        }
        if (options.story && options.location) {
            text += " The trip's story goes too, a paragraph a day" + (options.people_names
                ? "." : ", without the sentences that name people.");
        }
        return text;
    }

    /* The command that tries an export on this computer (export.serve_command:
       only this computer can open the site it serves; videos can be moved
       in, which python -m http.server does not allow): the summary's own
       (serve, with the program's whole path), else run in the program's
       folder. */
    function serveCommand(folder, summary) {
        if (summary && typeof summary.serve === "string" && summary.serve) {
            return summary.serve;
        }
        return "python export.py --serve \"" + String(folder || "") + "\"";
    }

    return {
        OPTIONS: OPTIONS,
        TYPES: TYPES,
        REASONS: REASONS,
        activeReasons: activeReasons,
        published: published,
        newFlags: newFlags,
        choose: choose,
        recount: recount,
        groups: groups,
        reasonWords: reasonWords,
        leftOut: leftOut,
        headline: headline,
        sizeText: sizeText,
        videoText: videoText,
        rawsText: rawsText,
        drivesText: drivesText,
        routesText: routesText,
        overlayText: overlayText,
        similarText: similarText,
        meaningText: meaningText,
        looksText: looksText,
        streetsText: streetsText,
        worldText: worldText,
        detailsText: detailsText,
        serveCommand: serveCommand,
        bytes: bytes
    };
})();

if (typeof module === "object" && module && module.exports) {
    module.exports = EXPORT_REVIEW;
}
