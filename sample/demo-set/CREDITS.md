# Demo Set Credits

The demo set is 39 pictures and 5 short videos that the published demo
(https://seligman.github.io/search_image_sample/) shows besides the 19
bundled sample pictures, to show more of what the program does: a trip,
videos, bursts and near-duplicates, text in pictures and varied scenes.
The bundled sample pictures are credited in
[../images/CREDITS.md](../images/CREDITS.md).

Every item is under a Creative Commons license or in the public domain:
CC BY items require attribution, which the tables below provide; CC0
items are dedicated to the public domain, and the Public Domain Mark item
has no known copyright. All of them come from Wikimedia Commons
(https://commons.wikimedia.org) or Flickr (https://www.flickr.com), and
each license was read on the source page itself in October 2026.

The files are not in this repository. `fetch_demo_set.py`, next to this
file, downloads the originals (about 564 MB) from their sources, checks
each against `manifest.json` (its size and SHA-256), and makes the copies
the demo uses (about 465 MB), with the changes listed under "Changes"
below. From the program folder:

    python sample/demo-set/fetch_demo_set.py --list
    python sample/demo-set/fetch_demo_set.py --out C:\Photos\DemoSet

A library cannot be inside the program folder, so give `--out` a folder
outside it (by default the copies go to `sample/demo-set/files`).
The originals stay in `sample/demo-set/originals` (or `--originals DIR`,
a folder of its own) so that a second run downloads only what is
missing; delete that folder when you are done.
`--only GROUP` (trip, videos, bursts, text or varied) fetches one group.
The videos need ffmpeg, as videos in a library do (see
[Videos](../../README.md#videos) in the main README); without it they are
skipped.

The license texts:
- CC BY 2.0: https://creativecommons.org/licenses/by/2.0/
- CC BY 4.0: https://creativecommons.org/licenses/by/4.0/
- CC0 1.0: https://creativecommons.org/publicdomain/zero/1.0/
- Public Domain Mark 1.0: https://creativecommons.org/publicdomain/mark/1.0/

## Trip: Porto

Fourteen pictures Michael Gaylard took in Porto on 22 to 24 April 2026; each
is also on his Flickr page (https://www.flickr.com/photos/mgaylard).

| File | Title | Creator | License | Source |
|------|-------|---------|---------|--------|
| trip-01-douro-buoy-freixo.jpg | The River Douro at Palácio do Freixo (55245790392) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:The_River_Douro_at_Pal%C3%A1cio_do_Freixo_%2855245790392%29.jpg) |
| trip-02-palacio-do-freixo.jpg | Palácio do Freixo (55245766332) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Pal%C3%A1cio_do_Freixo_%2855245766332%29.jpg) |
| trip-03-funicular-dos-guindais.jpg | Funicular dos Guindais (55247069635) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Funicular_dos_Guindais_%2855247069635%29.jpg) |
| trip-04-rua-miradouro-rooftops.jpg | Rua Miradouro (55246930474) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Rua_Miradouro_%2855246930474%29.jpg) |
| trip-05-ponte-maria-pia.jpg | Ponte Maria Pia (55245774217) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Ponte_Maria_Pia_%2855245774217%29.jpg) |
| trip-06-dom-luis-bridge-panorama.jpg | A panoramic view of the Dom Luís I Bridge spanning the Douro River in Porto, Portugal, with a yellow tram crossing and the historic city skyline in the background. (55246746406) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:A_panoramic_view_of_the_Dom_Lu%C3%ADs_I_Bridge_spanning_the_Douro_River_in_Porto%2C_Portugal%2C_with_a_yellow_tram_crossing_and_the_historic_city_skyline_in_the_background._%2855246746406%29.jpg) |
| trip-07-praca-gomes-teixeira.jpg | Praça de Gomes Teixeira (55246830238) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Pra%C3%A7a_de_Gomes_Teixeira_%2855246830238%29.jpg) |
| trip-08-riverside-market.jpg | Porto Riverside Market and Douro River Boats (55246688641) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Porto_Riverside_Market_and_Douro_River_Boats_%2855246688641%29.jpg) |
| trip-09-rabelo-boat.jpg | A Lively Riverboat Cruises Past the Colourful Buildings of Porto (55245785232) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:A_Lively_Riverboat_Cruises_Past_the_Colourful_Buildings_of_Porto_%2855245785232%29.jpg) |
| trip-10-stone-steps-alley.jpg | Weathered Stone Steps in a Narrow Alley (55247094930) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Weathered_Stone_Steps_in_a_Narrow_Alley_%2855247094930%29.jpg) |
| trip-11-colourful-corner.jpg | A Colourful Corner in Porto's Historic District (55245786552) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:A_Colourful_Corner_in_Porto%27s_Historic_District_%2855245786552%29.jpg) |
| trip-12-palacio-da-bolsa-arab-room.jpg | The Stunningly Ornate Mudejar Hall at the Palácio da Bolsa, Porto (55245889290) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:The_Stunningly_Ornate_Mudejar_Hall_at_the_Pal%C3%A1cio_da_Bolsa%2C_Porto_%2855245889290%29.jpg) |
| trip-13-ribeira-riverfront.jpg | Ribeira do Porto (55246830698) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Ribeira_do_Porto_%2855246830698%29.jpg) |
| trip-14-riverfront-at-night.jpg | Porto's Illuminated Riverfront and Historic Architecture at Night (55245778362) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Porto%27s_Illuminated_Riverfront_and_Historic_Architecture_at_Night_%2855245778362%29.jpg) |

## Videos

Five short videos. The two by Nesnad are dual-licensed (CC BY 4.0 and
GFDL); they are used under CC BY 4.0.

| File | Title | Creator | License | Source |
|------|-------|---------|---------|--------|
| videos-01-baltic-beach-waves.mp4 | Kühlungsborn Strand lub 2025-02-14 | Lukas Beck | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:K%C3%BChlungsborn_Strand_lub_2025-02-14.webm) |
| videos-02-dublin-snow-tram.mp4 | Dublin snow benburb st | MdeVicente | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Dublin_snow_benburb_st.webm) |
| videos-03-sika-deer-kushiro.mp4 | Yezo sika deer - kushiro hokkaido - 2021 Dec 9 | Nesnad | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Yezo_sika_deer_-_kushiro_hokkaido_-_2021_Dec_9.webm) |
| videos-04-train-shin-fuji-station.mp4 | Nemuro Main Line local train - shinfuji station 2021 12 10 | Nesnad | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Nemuro_Main_Line_local_train_-_shinfuji_station_2021_12_10.webm) |
| videos-05-maruia-falls.mp4 | Maruia Falls, Tasman, New Zealand | Mike Dickison | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Maruia_Falls%2C_Tasman%2C_New_Zealand.ogv) |

## Bursts and near-duplicates

Pictures taken moments apart, and two crops of one frame. The last is the
same frame as trip-05-ponte-maria-pia.jpg: its photographer uploaded it
twice.

| File | Title | Creator | License | Source |
|------|-------|---------|---------|--------|
| bursts-01-strokkur-bubble.jpg | Strokkur Geyser - Bubble (34418280801) | Theo Crazzolara | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Strokkur_Geyser_-_Bubble_%2834418280801%29.jpg) |
| bursts-02-strokkur-eruption.jpg | Strokkur Geyser (34461551446) | Theo Crazzolara | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Strokkur_Geyser_%2834461551446%29.jpg) |
| bursts-03-night-heron-a.jpg | Yellow-crowned Night-Heron (cropping-lightening) (31692581383) | Andy Reago & Chrissy McClarren | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Yellow-crowned_Night-Heron_%28cropping-lightening%29_%2831692581383%29.jpg) |
| bursts-04-night-heron-b.jpg | Yellow-crowned Night-Heron (cropping-lightening) (31692578673) | Andy Reago & Chrissy McClarren | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Yellow-crowned_Night-Heron_%28cropping-lightening%29_%2831692578673%29.jpg) |
| bursts-05-little-blue-heron-crop-a.jpg | Little Blue Heron (crop experimentation) (32333250002) | Andy Reago & Chrissy McClarren | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Little_Blue_Heron_(crop_experimentation)_(32333250002).jpg) |
| bursts-06-little-blue-heron-crop-b.jpg | Little Blue Heron (crop experimentation) (32363256441) | Andy Reago & Chrissy McClarren | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Little_Blue_Heron_(crop_experimentation)_(32363256441).jpg) |
| bursts-07-ponte-maria-pia-twin.jpg | Ponte Maria Pia (55246730866) | Michael Gaylard | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Ponte_Maria_Pia_%2855246730866%29.jpg) |

## Text in pictures

Signs, tickets, documents and a poster, for reading text in pictures.

| File | Title | Creator | License | Source |
|------|-------|---------|---------|--------|
| text-01-board-of-works-receipt.jpg | Board of Works receipt | Laineys Repertoire | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Flickr](https://www.flickr.com/photos/76283671@N00/157827178/) |
| text-02-vr-train-ticket.jpg | Train Ticket | Ananta Bhadra Lamichhane | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Flickr](https://www.flickr.com/photos/33088287@N00/2556077982/) |
| text-03-italian-specialities-sign.jpg | Hand Painted Sign, Copenhagen | Peter Lindberg | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Flickr](https://www.flickr.com/photos/35237094214@N01/189887777/) |
| text-04-hiking-signpost-goschenen.jpg | Uri - Göschenen | Kecko | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Flickr](https://www.flickr.com/photos/70981241@N00/35412066873/) |
| text-05-danger-deep-water-sign.jpg | Danger: deep water - hand painted sign | Futurilla | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Flickr](https://www.flickr.com/photos/52067454@N00/28498200495/) |
| text-06-circus-poster.jpg | Walter L. Main Circus Poster | Ashtabula Archive | [Public Domain Mark 1.0](https://creativecommons.org/publicdomain/mark/1.0/) | [Flickr](https://www.flickr.com/photos/115892967@N03/51762276826/) |
| text-07-militia-return.jpg | 1843 Boxborough Militia | liz west | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Flickr](https://www.flickr.com/photos/53133240@N00/101952116/) |
| text-08-trees-poem-book.jpg | Open book with “Trees,” Joyce Kilmer memorial, South Park, 2025-06-19 | Cbaile19 | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Open_book_with_%E2%80%9CTrees%2C%E2%80%9D_Joyce_Kilmer_memorial%2C_South_Park%2C_2025-06-19.jpg) |

## Varied scenes

The northern lights picture is credited as "FoxBill (Viljam Takis)":
its page names the photographer. The author of the Angkor Wat picture
asks to be credited by name, Supanut Arunoprayote.

| File | Title | Creator | License | Source |
|------|-------|---------|---------|--------|
| varied-01-aurora-comet-church.jpg | Comet C 2025 A6 Lemmon and Northen lights in Maarja-Magdaleena | FoxBill (Viljam Takis) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Comet_C_2025_A6_Lemmon_and_Northen_lights_in_Maarja-Magdaleena.jpg) |
| varied-02-jamaa-el-fna-dusk.jpg | Jamaa el Fna (7346063132) | Jorge Láscar | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Jamaa_el_Fna_%287346063132%29.jpg) |
| varied-03-lightning-storm-belfort.jpg | 2019-07-26 21-46-13 orage-Belfort | Thomas Bresson | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:2019-07-26_21-46-13_orage-Belfort.jpg) |
| varied-04-foggy-valley-witow.jpg | Zamglona dolina w Witowie w powiecie proszowickim, 20241020 0808 6322 | Jakub Hałun | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Zamglona_dolina_w_Witowie_w_powiecie_proszowickim%2C_20241020_0808_6322.jpg) |
| varied-05-kirkjufell-rainbow.jpg | Kirkjufell, Iceland, 20240714 1631 0713 | Jakub Hałun | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Kirkjufell%2C_Iceland%2C_20240714_1631_0713.jpg) |
| varied-06-angkor-wat-reflection.jpg | Angkor (I) | Supanut Arunoprayote | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Angkor_%28I%29.jpg) |
| varied-07-camels-gobi-dunes.jpg | Camels at Khongoryn Els 01 | Bernard Gagnon | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Camels_at_Khongoryn_Els_01.jpg) |
| varied-08-red-fox-drinking.jpg | Red Fox drinking from a lake in Gennevilliers, France | Alexis Lours | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Red_Fox_drinking_from_a_lake_in_Gennevilliers%2C_France.jpg) |
| varied-09-elephant-masai-mara.jpg | African bush elephant (Loxodonta africana), Masai Mara | Hobbyfotowiki | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:African_bush_elephant_%28Loxodonta_africana%29%2C_Masai_Mara.jpg) |
| varied-10-tekkadon-tuna-bowl.jpg | Tekkadon 001 | Ocdp | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Tekkadon_001.jpg) |

## Changes

The copies are the files as their creators published them, except for the
changes listed here. Resizing, re-encoding and adding metadata are
permitted by all the licenses listed. Every file not named below is
copied byte for byte as downloaded.

Resized: trip-07-praca-gomes-teixeira.jpg and trip-13-ribeira-riverfront.jpg
were resized from 6048x4032 to 1600x1067 pixels (their metadata kept), so
that passers-by in them stay unrecognisable.

Videos: the five videos were re-encoded to 720p H.264 MP4 copies (with
AAC sound), each holding its recording date as a QuickTime tag:
videos-01's own date, and for the others the date on the source page, with
12:00 as a placeholder time. videos-01 and videos-04 keep the GPS position
their phones recorded. The Dublin video's page date (1 March 2024) is
doubtful: Dublin's well-known 1 March snowfall was in 2018.

| File | Recording date written | Position kept |
|------|------------------------|---------------|
| videos-01-baltic-beach-waves.mp4 | 2025-02-14T16:17:07+0100 | +54.1538+011.6986-002.676/ |
| videos-02-dublin-snow-tram.mp4 | 2024-03-01T12:00:00+0000 | none |
| videos-03-sika-deer-kushiro.mp4 | 2021-12-09T12:00:00+0900 | none |
| videos-04-train-shin-fuji-station.mp4 | 2021-12-10T12:00:00+0900 | +43.0018+144.3500/ |
| videos-05-maruia-falls.mp4 | 2019-06-09T12:00:00+1200 | none |

Positions added: where a picture's file had no position of its own and its
source page gives one (a Flickr geotag, or the camera location on its
Wikimedia Commons page), it was written into the copy as EXIF GPS. A
Flickr geotag also carries its accuracy, as GPSHPositioningError, as the
bundled sample's do: Flickr records the map zoom level a geotag was set
at, 16 (street level) taken as about 100 metres and 13 (town level) as
about 800 metres. The red fox picture's camera had also recorded that it
had no position (GPSStatus "V", void); that status was removed when the
geotag was added.

| File | Position written | From the source page | Accuracy written |
|------|------------------|----------------------|------------------|
| trip-12-palacio-da-bolsa-arab-room.jpg | 41.141371, -8.615879 | Flickr geotag, accuracy 16 (street level) | 100 m |
| text-02-vr-train-ticket.jpg | 60.866019, 26.698837 | Flickr geotag, accuracy 13 (town level) | 800 m |
| text-04-hiking-signpost-goschenen.jpg | 46.66595, 8.58808 | Flickr geotag, accuracy 16 (street level) | 100 m |
| varied-01-aurora-comet-church.jpg | 58.60882, 26.74238 | camera location on the Commons page | none (not stated) |
| varied-02-jamaa-el-fna-dusk.jpg | 31.625833, -7.989444 | Flickr geotag, accuracy 16 (street level) | 100 m |
| varied-04-foggy-valley-witow.jpg | 50.1495, 20.57623906 | camera location on the Commons page | none (not stated) |
| varied-05-kirkjufell-rainbow.jpg | 64.92596171, -23.31226149 | camera location on the Commons page | none (not stated) |
| varied-06-angkor-wat-reflection.jpg | 13.411947, 103.864062 | camera location on the Commons page | none (not stated) |
| varied-07-camels-gobi-dunes.jpg | 43.795278, 102.198944 | camera location on the Commons page | none (not stated) |
| varied-08-red-fox-drinking.jpg | 48.935269, 2.314757 | Flickr geotag, accuracy 16 (street level) | 100 m |
| varied-10-tekkadon-tuna-bowl.jpg | 34.850992, 138.326097 | camera location on the Commons page | none (not stated) |

Dates added: three pictures had no capture date (varied-01 only the date
it was edited); the date their source pages give was written into the copy
as EXIF DateTimeOriginal.

| File | Date written | From the source page |
|------|--------------|----------------------|
| text-01-board-of-works-receipt.jpg | 2007-05-01 00:00:00 | Flickr's "Taken on" date; probably when the receipt (dated 1918) was scanned or uploaded |
| text-04-hiking-signpost-goschenen.jpg | 2017-07-23 13:46:23 | Flickr's "Taken on" date and time |
| varied-01-aurora-comet-church.jpg | 2025-10-18 20:00:00 +03:00 | the page's date (its description says "the same evening"); the hour is a placeholder |

Place names added: where a picture's page names only a place, the names
were written in the copy's XMP location fields (City, State, Country and
the country code), with no position: the place is shown and found, but
has no pin on the map, as a pin at a town's centre would look more exact
than the page is. A region or country the page leaves out but its place
implies is named as the project's place names (GeoNames) name it.

| File | Names written | From the source page |
|------|---------------|----------------------|
| bursts-01-strokkur-bubble.jpg | Iceland (IS) | Strokkur geyser, Iceland (title 'Strokkur Geyser'); no coordinates |
| bursts-02-strokkur-eruption.jpg | Iceland (IS) | Strokkur geyser, Iceland (title 'Strokkur Geyser'); no coordinates |
| bursts-03-night-heron-a.jpg | St. Louis, Missouri, United States (US) | Forest Park, St. Louis, Missouri, USA (description 'Forest Park 5/9/16'; Commons category 'Birds of Forest Park (St. Louis)') |
| bursts-04-night-heron-b.jpg | St. Louis, Missouri, United States (US) | Forest Park, St. Louis, Missouri, USA (as bursts-03-night-heron-a.jpg) |
| bursts-05-little-blue-heron-crop-a.jpg | St. Louis, Missouri, United States (US) | Carondelet Park, St. Louis, Missouri, USA (description 'Carondelet Park 4/4/16') |
| bursts-06-little-blue-heron-crop-b.jpg | St. Louis, Missouri, United States (US) | Carondelet Park, St. Louis, Missouri, USA (description 'Carondelet Park 4/4/16') |
| text-03-italian-specialities-sign.jpg | Copenhagen, Capital Region, Denmark (DK) | Copenhagen, Denmark (title) |
| text-05-danger-deep-water-sign.jpg | Stoke-on-Trent, England, United Kingdom (GB) | Middleport Pottery canalside, Stoke-on-Trent, England (description) |
| text-08-trees-poem-book.jpg | South Park, Pennsylvania, United States (US) | Joyce Kilmer memorial, South Park (Allegheny County, Pennsylvania), USA (title); no coordinates |
| varied-09-elephant-masai-mara.jpg | Kenya (KE) | Masai Mara, Kenya (title); no coordinates |

Place name corrected: the photographer's own XMP location fields of
trip-13-ribeira-riverfront.jpg and trip-14-riverfront-at-night.jpg name
the region "Noorte Region" (photoshop:State); their copies hold "Norte
Region", as his other Porto pictures do. (The same misspelling in their
older IPTC fields, which the program does not read, is left as it is.)
