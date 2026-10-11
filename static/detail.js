/* Detail page: shows metadata, EXIF, and the AI description for one image. */
(function () {
    "use strict";

    function imageId() {
        var match = /[?&]id=([^&]+)/.exec(window.location.search);
        return match ? decodeURIComponent(match[1]) : null;
    }

    function humanSize(bytes) {
        var units = ["B", "KB", "MB", "GB"];
        var value = bytes;
        var unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value = value / 1024;
            unit = unit + 1;
        }
        return (unit === 0 ? value : value.toFixed(1)) + " " + units[unit];
    }

    function whenText(seconds) {
        if (!seconds) {
            return "";
        }
        return new Date(seconds * 1000).toLocaleString();
    }

    // The capture time ("YYYY-MM-DDTHH:MM:SS", local to where the photo was
    // taken). Empty when it only repeats the file's modified time.
    function takenText(rec) {
        var match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/.exec(rec.taken_at || "");
        if (!match || rec.taken_source === "mtime") {
            return "";
        }
        var when = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]),
            Number(match[4]), Number(match[5]), Number(match[6]));
        var text = when.toLocaleString();
        if (rec.tz_offset) {
            text += " (UTC" + rec.tz_offset + ")";
        }
        if (rec.taken_source === "filename") {
            text += " (from the file name)";
        }
        return text;
    }

    // A line for 360-degree photos (their preview is the panorama).
    function projectionText(projection) {
        if (projection === "dual_fisheye") {
            return "Yes (two fisheye lenses, stitched into a panorama for the preview)";
        }
        if (projection === "equirectangular") {
            return "Yes (equirectangular panorama)";
        }
        return "";
    }

    function factsTable(pairs) {
        var table = document.createElement("table");
        table.className = "facts";
        pairs.forEach(function (pair) {
            if (pair[1] === "" || pair[1] === null || pair[1] === undefined) {
                return;
            }
            var tr = document.createElement("tr");
            var key = document.createElement("td");
            key.className = "key";
            key.textContent = pair[0];
            var value = document.createElement("td");
            value.textContent = String(pair[1]);
            tr.appendChild(key);
            tr.appendChild(value);
            table.appendChild(tr);
        });
        return table;
    }

    function section(title, node) {
        var wrap = document.createElement("div");
        wrap.className = "section";
        var heading = document.createElement("h2");
        heading.textContent = title;
        wrap.appendChild(heading);
        wrap.appendChild(node);
        return wrap;
    }

    function chips(values) {
        var box = document.createElement("div");
        box.className = "chips";
        values.forEach(function (value) {
            var chip = document.createElement("span");
            chip.className = "chip";
            chip.textContent = value;
            box.appendChild(chip);
        });
        return box;
    }

    function paragraph(text) {
        var node = document.createElement("p");
        node.textContent = text;
        return node;
    }

    function renderImage(body, rec) {
        var frame = document.createElement("div");
        frame.className = "detail-image";
        var link = document.createElement("a");
        link.href = SITE.imageUrl(rec);
        var img = document.createElement("img");
        var thumb = SITE.thumbUrl(rec);
        var preview = SITE.previewUrl(rec);
        if (preview) {
            // No preview can be made when the original is offline or
            // unreadable; show the thumbnail instead.
            img.addEventListener("error", function onError() {
                img.removeEventListener("error", onError);
                img.src = thumb;
            });
        }
        img.src = preview || thumb;
        img.alt = rec.name;
        link.appendChild(img);
        frame.appendChild(link);
        body.appendChild(frame);

        var hint = paragraph("Click the image to view it full size.");
        hint.className = "hint";
        body.appendChild(hint);

        var name = document.createElement("div");
        name.className = "detail-name";
        name.textContent = rec.name;
        body.appendChild(name);
    }

    // Older (version 4) descriptions: scores from 0 to 1.
    var OLD_SCORE_LABELS = [
        ["aesthetic_score", "Aesthetic"],
        ["sharpness", "Sharpness"],
        ["exposure_score", "Exposure"],
        ["watermark_score", "Watermark-free"]
    ];
    // Version 5 descriptions: whole numbers from 1 to 5.
    var SCORE_LABELS = [
        ["aesthetic", "Aesthetic"],
        ["sharpness", "Sharpness"],
        ["exposure", "Exposure"]
    ];

    var PHOTO_TYPES = {
        photo: "Photo", screenshot: "Screenshot", document: "Document", scan: "Scan",
        artwork: "Artwork", meme: "Meme", diagram: "Diagram", other: "Other"
    };
    var DOCUMENT_TYPES = {
        web: "web page", app: "app screen", card_or_id: "card or ID", book_page: "book page"
    };
    var SETTINGS = { indoor: "Indoors", outdoor: "Outdoors", vehicle: "In a vehicle" };
    var TIMES = { day: "day", sunrise_sunset: "sunrise or sunset", night: "night" };
    var PEOPLE = {
        "1": "1 person", "2": "2 people", "3-5": "3 to 5 people", "6-10": "6 to 10 people",
        crowd: "a crowd"
    };
    var FLAGS = {
        blurry: "Blurry", dark: "Dark", overexposed: "Overexposed", accidental: "Accidental",
        screen_photo: "Photo of a screen"
    };
    var SENSITIVE = {
        suggestive: "Suggestive", explicit: "Explicit", graphic: "Graphic content"
    };

    function words(value) {
        return String(value).replace(/_/g, " ");
    }

    function capitalized(text) {
        return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
    }

    function label(map, value) {
        if (typeof value !== "string" || !value) {
            return "";
        }
        return Object.prototype.hasOwnProperty.call(map, value) ? map[value] : words(value);
    }

    function list(value) {
        return Array.isArray(value) ? value.filter(function (item) {
            return typeof item === "string" && item;
        }) : [];
    }

    function scoresTable(info) {
        // Both versions have "sharpness"; only version 4 has these.
        var old = ["aesthetic_score", "exposure_score", "watermark_score"].some(function (key) {
            return Object.prototype.hasOwnProperty.call(info, key);
        });
        var pairs = [];
        (old ? OLD_SCORE_LABELS : SCORE_LABELS).forEach(function (entry) {
            var value = info[entry[0]];
            if (typeof value === "number" && isFinite(value)) {
                pairs.push([entry[1], old ? value.toFixed(2) : Math.round(value) + "/5"]);
            }
        });
        return pairs.length ? factsTable(pairs) : null;
    }

    // Markers for pictures that should not be shared casually.
    function markers(info) {
        var box = document.createElement("div");
        box.className = "chips";
        var sensitive = label(SENSITIVE, info.sensitive === "none" ? "" : info.sensitive);
        if (sensitive) {
            var badge = document.createElement("span");
            badge.className = "chip badge-warn";
            badge.textContent = "Sensitive: " + sensitive;
            box.appendChild(badge);
        }
        if (info.private_info === true) {
            var privateBadge = document.createElement("span");
            privateBadge.className = "chip badge-warn";
            privateBadge.textContent = "Private information";
            box.appendChild(privateBadge);
        }
        return box.childNodes.length ? box : null;
    }

    function aboutTable(info) {
        var type = label(PHOTO_TYPES, info.photo_type);
        if (info.document_type && info.document_type !== "none") {
            var documentType = label(DOCUMENT_TYPES, info.document_type);
            type = type ? type + " (" + documentType + ")" : capitalized(documentType);
        }
        var place = [];
        if (info.setting && info.setting !== "unknown") {
            place.push(label(SETTINGS, info.setting));
        }
        if (typeof info.scene === "string" && info.scene) {
            place.push(info.scene);
        }
        if (info.time_of_day && info.time_of_day !== "unknown") {
            place.push(label(TIMES, info.time_of_day));
        }
        var people = "";
        if (info.people && info.people !== "none") {
            people = capitalized(label(PEOPLE, info.people));
        } else if (info.people === "none") {
            people = "None";
        }
        var descriptors = list(info.people_descriptors);
        if (descriptors.length) {
            people = (people ? people + ": " : "") + descriptors.join(", ");
        }
        var pairs = [
            ["Type", type],
            ["Setting", capitalized(place.join(" · "))],
            ["Event", info.event && info.event !== "none" ? capitalized(words(info.event)) : ""],
            ["People", people],
            ["Mood", info.mood && info.mood !== "neutral" ? capitalized(words(info.mood)) : ""],
            ["Place (a guess)",
                typeof info.location_guess === "string" ? info.location_guess : ""],
            ["Text language", typeof info.text_language === "string" ? info.text_language : ""]
        ];
        var shown = pairs.filter(function (pair) { return pair[1]; });
        return shown.length ? factsTable(shown) : null;
    }

    function objectChips(value) {
        if (!Array.isArray(value)) {
            return [];
        }
        return value.filter(function (item) {
            return item && typeof item.name === "string" && item.name;
        }).map(function (item) {
            var count = typeof item.count === "number" && item.count > 1 ? " ×" + item.count : "";
            return item.name + count;
        });
    }

    function credit(rec, info) {
        var provenance = rec.provenance || {};
        var model = provenance.model || info._model_name;
        if (!model) {
            return null;
        }
        var text = "Described by " + model;
        if (provenance.endpoint) {
            text += " via " + provenance.endpoint;
        }
        var node = paragraph(text);
        node.className = "hint";
        return node;
    }

    function renderDescription(body, rec) {
        var info = rec.info;
        if (!info) {
            var none = paragraph("This image has not been described yet.");
            none.className = "hint";
            body.appendChild(section("Description", none));
            return;
        }
        var block = document.createElement("div");
        if (typeof info.title === "string" && info.title) {
            var title = document.createElement("div");
            title.className = "detail-title";
            title.textContent = info.title;
            block.appendChild(title);
        }
        var badges = markers(info);
        if (badges) {
            block.appendChild(badges);
        }
        block.appendChild(paragraph(info.description || ""));
        var about = aboutTable(info);
        if (about) {
            block.appendChild(about);
        }
        [
            ["Subjects", list(info.subjects)],
            ["Tags", list(info.tags)],
            ["Animals", list(info.animals)],
            ["Food", list(info.food)],
            ["Vehicles", list(info.vehicles)],
            ["Objects", objectChips(info.objects)],
            ["Activities", list(info.activities)],
            ["Landmarks", list(info.landmarks)],
            ["Colors", list(info.colors)],
            ["Quality", list(info.quality_flags).map(function (flag) {
                return label(FLAGS, flag);
            })]
        ].forEach(function (entry) {
            if (entry[1].length) {
                block.appendChild(section(entry[0], chips(entry[1])));
            }
        });
        if (info.text) {
            block.appendChild(section("Text in image", paragraph(info.text)));
        }
        var scores = scoresTable(info);
        if (scores) {
            block.appendChild(section("Scores", scores));
        }
        var line = credit(rec, info);
        if (line) {
            block.appendChild(line);
        }
        body.appendChild(section("Description", block));
    }

    function renderExif(body, exif) {
        var pairs = [];
        var gps = null;
        Object.keys(exif || {}).forEach(function (key) {
            if (key === "GPS") {
                gps = exif[key];
                return;
            }
            var value = exif[key];
            if (Array.isArray(value)) {
                value = value.join(", ");
            }
            pairs.push([key, value]);
        });
        if (pairs.length) {
            body.appendChild(section("EXIF", factsTable(pairs)));
        }
        if (gps) {
            var gpsPairs = Object.keys(gps).map(function (key) {
                var value = gps[key];
                if (Array.isArray(value)) {
                    value = value.join(", ");
                }
                return [key, value];
            });
            body.appendChild(section("GPS", factsTable(gpsPairs)));
        }
    }

    function render(rec) {
        var body = document.getElementById("detail-body");
        body.innerHTML = "";
        renderImage(body, rec);
        body.appendChild(section("File", factsTable([
            ["Library", rec.library],
            ["Path", rec.path],
            ["Dimensions", rec.width + " x " + rec.height + " pixels"],
            ["360\u00b0 photo", projectionText(rec.projection)],
            ["Size", humanSize(rec.size)],
            ["Taken", takenText(rec)],
            ["Camera", rec.camera],
            ["Modified", whenText(rec.mtime)]
        ])));
        renderDescription(body, rec);
        renderExif(body, rec.exif);
    }

    function init() {
        document.getElementById("back").href = SITE.homeUrl();
        var id = imageId();
        var body = document.getElementById("detail-body");
        if (!id) {
            body.textContent = "No image selected.";
            return;
        }
        SITE.loadRecord(id, function (rec) {
            if (!rec) {
                body.textContent = "Image not found.";
                return;
            }
            render(rec);
        });
    }

    document.addEventListener("DOMContentLoaded", init);
})();
