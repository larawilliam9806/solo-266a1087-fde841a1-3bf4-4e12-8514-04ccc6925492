from __future__ import annotations

from tests.test_api import full_setup, make_film, make_reel, put_edit, put_subtitles, seg


def _publish(client, film):
    delivery = client.post(f"/films/{film}/deliveries").json()
    response = client.post(f"/deliveries/{delivery['id']}/publish")
    assert response.status_code == 200, response.text
    return response.json()


def _republish(client, film):
    return _publish(client, film)


def test_diff_by_output_versions_reports_added_deleted_and_changes(client):
    reel, film = full_setup(client)
    first = _publish(client, film)
    assert first["output_version"] == 1

    # Re-cut: drop line 1 entirely, move line 2, keep line 3, add a new line 4.
    put_subtitles(
        client,
        reel,
        [
            {"number": 2, "start_ms": 3_000, "end_ms": 4_000, "text": "第二句改字"},
            {"number": 3, "start_ms": 5_000, "end_ms": 6_000, "text": "第三句"},
            {"number": 4, "start_ms": 7_000, "end_ms": 8_000, "text": "第四句"},
        ],
    )
    put_edit(
        client,
        film,
        [
            seg(reel, 3_000, 6_000, 10_000),
            seg(reel, 7_000, 8_000, 13_000),
        ],
    )
    second = _republish(client, film)
    assert second["output_version"] == 2

    response = client.get(f"/films/{film}/publications/diff?base=1&target=2")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["film_id"] == film
    assert body["base"]["output_version"] == 1
    assert body["target"]["output_version"] == 2
    assert body["summary"]["added"] >= 1
    assert body["summary"]["deleted"] >= 1
    assert body["summary"]["changed"] >= 1

    deleted_lines = {entry["line_number"] for entry in body["deleted"]}
    assert 1 in deleted_lines
    assert all(entry["only_in"] == "base" for entry in body["deleted"])
    assert all(entry["only_in"] == "target" for entry in body["added"])

    changed = {entry["line_number"]: entry for entry in body["changed"]}
    assert 2 in changed
    assert "text" in changed[2]["changes"]
    assert "time" in changed[2]["changes"]
    assert 3 in changed  # line 3 moved in finished-film timecode
    assert "time" in changed[3]["changes"]
    assert "text" not in changed[3]["changes"]


def test_diff_by_publication_ids_matches_same_data(client):
    reel, film = full_setup(client)
    first = _publish(client, film)

    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 1_000, "end_ms": 2_000, "text": "改后"}],
    )
    second = _republish(client, film)

    by_versions = client.get(f"/films/{film}/publications/diff?base=1&target=2").json()
    by_ids = client.get(
        f"/publications/diff?base={first['id']}&target={second['id']}"
    )
    assert by_ids.status_code == 200, by_ids.text
    ids_body = by_ids.json()

    assert ids_body["summary"] == by_versions["summary"]
    assert ids_body["changed"] == by_versions["changed"]
    assert ids_body["base"]["publication_id"] == first["id"]
    assert ids_body["target"]["publication_id"] == second["id"]


def test_diffing_a_version_against_itself_is_all_unchanged(client):
    reel, film = full_setup(client)
    _publish(client, film)

    body = client.get(f"/films/{film}/publications/diff?base=1&target=1").json()
    assert body["summary"] == {
        "added": 0,
        "deleted": 0,
        "changed": 0,
        "unchanged": 3,
    }


def test_unknown_output_version_is_refused(client):
    _, film = full_setup(client)
    _publish(client, film)

    response = client.get(f"/films/{film}/publications/diff?base=1&target=9")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "publication_version_not_found"
    assert error["details"] == {"film_id": film, "output_version": 9}


def test_output_version_without_any_publication_is_refused(client):
    _, film = full_setup(client)
    response = client.get(f"/films/{film}/publications/diff?base=1&target=2")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "publication_version_not_found"


def test_unknown_publication_id_is_refused(client):
    _, film = full_setup(client)
    published = _publish(client, film)

    response = client.get(f"/publications/diff?base={published['id']}&target=999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "publication_not_found"


def test_open_delivery_creates_no_publication_version(client):
    _, film = full_setup(client)
    # An open delivery reserves no output version: only version 1 exists once a
    # successful publish happens, and version 2 must be refused meanwhile.
    client.post(f"/films/{film}/deliveries")
    published = _publish(client, film)
    assert published["output_version"] == 1

    response = client.get(f"/films/{film}/publications/diff?base=1&target=2")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "publication_version_not_found"


def test_rejected_delivery_consumes_no_output_version(client):
    reel, film = full_setup(client)
    delivery = client.post(f"/films/{film}/deliveries").json()
    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 1_000, "end_ms": 2_000, "text": "换版"}],
    )
    rejected = client.post(f"/deliveries/{delivery['id']}/publish")
    assert rejected.status_code == 409

    # The rejection wrote no frozen snapshot, so no output version exists at all.
    response = client.get(f"/films/{film}/publications/diff?base=1&target=1")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "publication_version_not_found"

    # A fresh, successful delivery becomes output version 1, then diffs work.
    published = _publish(client, film)
    assert published["output_version"] == 1
    ok = client.get(f"/films/{film}/publications/diff?base=1&target=1")
    assert ok.status_code == 200


def test_cross_film_publications_are_refused(client):
    reel_a, film_a = full_setup(client, reel_name="ra", film_name="fa")
    pub_a = _publish(client, film_a)

    reel_b = make_reel(client, "rb")
    put_subtitles(
        client,
        reel_b,
        [{"number": 1, "start_ms": 0, "end_ms": 1_000, "text": "B片台词"}],
    )
    film_b = make_film(client, "fb")
    put_edit(client, film_b, [seg(reel_b, 0, 1_000, 0)])
    pub_b = _publish(client, film_b)

    response = client.get(
        f"/publications/diff?base={pub_a['id']}&target={pub_b['id']}"
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "publications_from_different_films"
    assert error["details"]["base"]["film_id"] == film_a
    assert error["details"]["target"]["film_id"] == film_b


def test_diff_reads_frozen_snapshots_and_ignores_later_versions(client):
    reel, film = full_setup(client)
    first = _publish(client, film)

    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 1_000, "end_ms": 2_000, "text": "第二版"}],
    )
    second = _republish(client, film)

    before = client.get(
        f"/publications/diff?base={first['id']}&target={second['id']}"
    ).json()

    # More re-versions after both publications must not change the frozen diff.
    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 1_000, "end_ms": 2_000, "text": "第三版"}],
    )
    put_edit(client, film, [seg(reel, 0, 2_000, 99_000)])

    after = client.get(
        f"/publications/diff?base={first['id']}&target={second['id']}"
    ).json()
    assert after == before


def test_type_change_between_cue_and_review_is_reported(client):
    reel = make_reel(client)
    put_subtitles(
        client,
        reel,
        [{"number": 7, "start_ms": 0, "end_ms": 2_000, "text": "整句"}],
    )
    film = make_film(client)

    # First cut keeps the whole line -> cue.
    put_edit(client, film, [seg(reel, 0, 2_000, 0)])
    first = _publish(client, film)

    # Re-cut keeps only half of it -> review item.
    put_edit(client, film, [seg(reel, 0, 1_000, 0)])
    second = _republish(client, film)

    body = client.get(
        f"/publications/diff?base={first['id']}&target={second['id']}"
    ).json()
    assert body["summary"]["changed"] == 1
    entry = body["changed"][0]
    assert entry["base"]["type"] == "cue"
    assert entry["target"]["type"] == "review"
    assert entry["changes"]["type"] == {"base": "cue", "target": "review"}
    assert entry["changes"]["time"]["target"] == {
        "start_ms": 0,
        "end_ms": 1_000,
    }
    assert entry["target"]["keep_end_ms"] == 1_000


def test_reused_clip_occurrences_are_matched_in_order(client):
    reel = make_reel(client)
    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 0, "end_ms": 1_000, "text": "重复句"}],
    )
    film = make_film(client)

    put_edit(
        client,
        film,
        [seg(reel, 0, 1_000, 0), seg(reel, 0, 1_000, 5_000)],
    )
    _publish(client, film)

    # Second occurrence shifts; the first occurrence stays put.
    put_edit(
        client,
        film,
        [seg(reel, 0, 1_000, 0), seg(reel, 0, 1_000, 8_000)],
    )
    _republish(client, film)

    body = client.get(f"/films/{film}/publications/diff?base=1&target=2").json()
    assert body["summary"]["unchanged"] == 1
    assert body["summary"]["changed"] == 1
    changed = body["changed"][0]
    assert changed["line_number"] == 1
    assert changed["occurrence"] == 1
    assert changed["changes"]["time"]["target"]["start_ms"] == 8_000


def test_missing_or_non_positive_query_params_are_422(client):
    _, film = full_setup(client)
    _publish(client, film)

    missing = client.get(f"/films/{film}/publications/diff?base=1")
    assert missing.status_code == 422

    non_positive = client.get(f"/films/{film}/publications/diff?base=0&target=1")
    assert non_positive.status_code == 422

    bad_id = client.get("/publications/diff?base=1&target=-2")
    assert bad_id.status_code == 422


def test_diff_route_does_not_swallow_single_publication_lookup(client):
    _, film = full_setup(client)
    published = _publish(client, film)

    single = client.get(f"/publications/{published['id']}")
    assert single.status_code == 200
    assert single.json()["id"] == published["id"]
