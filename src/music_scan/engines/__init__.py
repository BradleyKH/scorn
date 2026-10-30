from music_scan.engines import audiveris, homr

ENGINES = {
    "audiveris": audiveris.scan,
    "homr": homr.scan,
}
