from __future__ import annotations


def make_reel(client, name="reel-a"):
    response = client.post("/source-reels", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def put_subtitles(client, reel_id, lines):
    response = client.post(
        f"/source-reels/{reel_id}/subtitles", json={"lines": lines}
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_film(client, name="film-a"):
    response = client.post("/films", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def put_edit(client, film_id, segments):
    response = client.post(f"/films/{film_id}/edit-tables", json={"segments": segments})
    assert response.status_code == 201, response.text
    return response.json()


def seg(reel, s, e, out):
    return {
        "source_reel_id": reel,
        "source_start_ms": s,
        "source_end_ms": e,
        "output_start_ms": out,
    }


def full_setup(client, *, reel_name="r1", film_name="f1"):
    reel = make_reel(client, reel_name)
    put_subtitles(
        client,
        reel,
        [
            {"number": 1, "start_ms": 1_000, "end_ms": 2_000, "text": "第一句"},
            {"number": 2, "start_ms": 3_000, "end_ms": 4_000, "text": "第二句"},
            {"number": 3, "start_ms": 5_000, "end_ms": 6_000, "text": "第三句"},
        ],
    )
    film = make_film(client, film_name)
    segments = [
        seg(reel, 0, 4_000, 0),
        seg(reel, 5_000, 7_000, 4_000),
    ]
    put_edit(client, film, segments)
    return reel, film


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_full_flow_retains_whole_lines_and_flags_partial(client):
    reel, film = full_setup(client)

    created = client.post(f"/films/{film}/deliveries")
    assert created.status_code == 201, created.text
    delivery = created.json()
    assert delivery["state"] == "open"
    assert delivery["edit_table_version"] == 1
    assert delivery["sources"] == [
        {"source_reel_id": reel, "source_reel_name": "r1", "subtitle_version": 1}
    ]

    preview = client.get(f"/deliveries/{delivery['id']}/preview").json()
    assert [(c["line_number"], c["start_ms"], c["end_ms"]) for c in preview["subtitles"]] == [
        (1, 1_000, 2_000),
        (2, 3_000, 4_000),
        (3, 4_000, 5_000),
    ]
    assert preview["reviews"] == []

    published = client.post(f"/deliveries/{delivery['id']}/publish")
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["output_version"] == 1
    assert [c["text"] for c in body["subtitles"]] == ["第一句", "第二句", "第三句"]

    latest = client.get(f"/films/{film}/subtitles/latest").json()
    assert latest["output_version"] == 1
    assert [(s["start_ms"], s["end_ms"]) for s in latest["subtitles"]] == [
        (1_000, 2_000),
        (3_000, 4_000),
        (4_000, 5_000),
    ]
    assert latest["reviews"] == []


def test_partial_line_lists_source_reel_line_and_retained_range(client):
    reel = make_reel(client)
    put_subtitles(
        client,
        reel,
        [{"number": 7, "start_ms": 1_000, "end_ms": 3_000, "text": "半句台词"}],
    )
    film = make_film(client)
    put_edit(client, film, [seg(reel, 0, 2_000, 10_000)])

    delivery = client.post(f"/films/{film}/deliveries").json()
    published = client.post(f"/deliveries/{delivery['id']}/publish")
    assert published.status_code == 200, published.text
    body = published.json()

    assert body["subtitles"] == []
    assert body["reviews"] == [
        {
            "source_reel_id": reel,
            "source_reel_name": "reel-a",
            "line_number": 7,
            "text": "半句台词",
            "keep_start_ms": 11_000,
            "keep_end_ms": 12_000,
        }
    ]


def test_line_split_across_two_gaps_produces_two_review_items(client):
    reel = make_reel(client)
    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 0, "end_ms": 3_000, "text": "跨洞"}],
    )
    film = make_film(client)
    put_edit(
        client,
        film,
        [seg(reel, 0, 1_000, 0), seg(reel, 2_000, 3_000, 1_000)],
    )

    delivery = client.post(f"/films/{film}/deliveries").json()
    body = client.post(f"/deliveries/{delivery['id']}/publish").json()

    assert [(r["keep_start_ms"], r["keep_end_ms"]) for r in body["reviews"]] == [
        (0, 1_000),
        (1_000, 2_000),
    ]


def test_contiguous_segments_merge_and_keep_whole_line(client):
    reel = make_reel(client)
    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 500, "end_ms": 1_500, "text": "跨段"}],
    )
    film = make_film(client)
    put_edit(
        client,
        film,
        [seg(reel, 0, 1_000, 0), seg(reel, 1_000, 2_000, 1_000)],
    )

    delivery = client.post(f"/films/{film}/deliveries").json()
    body = client.post(f"/deliveries/{delivery['id']}/publish").json()

    assert body["reviews"] == []
    assert [(c["start_ms"], c["end_ms"]) for c in body["subtitles"]] == [(500, 1_500)]


def test_reused_clip_yields_one_subtitle_per_occurrence(client):
    reel = make_reel(client)
    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 0, "end_ms": 2_000, "text": "重复"}],
    )
    film = make_film(client)
    put_edit(
        client,
        film,
        [seg(reel, 0, 2_000, 0), seg(reel, 0, 2_000, 10_000)],
    )

    delivery = client.post(f"/films/{film}/deliveries").json()
    body = client.post(f"/deliveries/{delivery['id']}/publish").json()

    assert [(c["start_ms"], c["end_ms"]) for c in body["subtitles"]] == [
        (0, 2_000),
        (10_000, 12_000),
    ]
    assert [c["line_number"] for c in body["subtitles"]] == [1, 1]


def test_new_subtitle_version_between_delivery_and_publish_rejects(client):
    reel, film = full_setup(client)
    delivery = client.post(f"/films/{film}/deliveries").json()

    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 1_000, "end_ms": 2_000, "text": "改过的第一句"}],
    )

    response = client.post(f"/deliveries/{delivery['id']}/publish")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "delivery_stale"
    assert error["changes"]["sources"] == [
        {"source_reel_id": reel, "pinned": 1, "current": 2}
    ]

    state = client.get(f"/deliveries/{delivery['id']}").json()
    assert state["state"] == "rejected"
    assert state["output_version"] is None

    assert client.get(f"/films/{film}/subtitles/latest").status_code == 404


def test_new_edit_version_between_delivery_and_publish_rejects(client):
    reel, film = full_setup(client)
    delivery = client.post(f"/films/{film}/deliveries").json()

    put_edit(client, film, [seg(reel, 0, 3_000, 0)])

    response = client.post(f"/deliveries/{delivery['id']}/publish")
    assert response.status_code == 409
    changes = response.json()["error"]["changes"]
    assert changes["edit_table"] == {"pinned": 1, "current": 2}
    assert "sources" not in changes


def test_unrelated_reel_change_does_not_block_publish(client):
    reel_a, film = full_setup(client)
    other = make_reel(client, "unrelated")
    put_subtitles(
        client,
        other,
        [{"number": 1, "start_ms": 0, "end_ms": 100, "text": "别的片"}],
    )

    delivery = client.post(f"/films/{film}/deliveries").json()
    response = client.post(f"/deliveries/{delivery['id']}/publish")
    assert response.status_code == 200, response.text


def test_changes_while_open_cause_rejection_and_a_fresh_delivery_succeeds(client):
    reel, film = full_setup(client)
    first = client.post(f"/films/{film}/deliveries").json()

    put_subtitles(
        client,
        reel,
        [
            {"number": 1, "start_ms": 1_000, "end_ms": 2_000, "text": "第一句 v2"},
            {"number": 2, "start_ms": 3_000, "end_ms": 4_000, "text": "第二句 v2"},
            {"number": 3, "start_ms": 5_000, "end_ms": 6_000, "text": "第三句 v2"},
        ],
    )
    put_edit(client, film, [seg(reel, 0, 7_000, 0)])

    stale = client.get(f"/deliveries/{first['id']}").json()
    assert stale["stale"] is True

    rejected = client.post(f"/deliveries/{first['id']}/publish")
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "delivery_stale"

    second = client.post(f"/films/{film}/deliveries")
    assert second.status_code == 201, second.text
    second_body = second.json()
    assert second_body["stale"] is False
    assert second_body["edit_table_version"] == 2
    assert second_body["sources"][0]["subtitle_version"] == 2

    published = client.post(f"/deliveries/{second_body['id']}/publish")
    assert published.status_code == 200, published.text
    assert [c["text"] for c in published.json()["subtitles"]] == [
        "第一句 v2",
        "第二句 v2",
        "第三句 v2",
    ]


def test_multiple_open_deliveries_may_coexist(client):
    _, film = full_setup(client)
    first = client.post(f"/films/{film}/deliveries")
    second = client.post(f"/films/{film}/deliveries")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert client.post(f"/deliveries/{first.json()['id']}/publish").status_code == 200
    assert client.post(f"/deliveries/{second.json()['id']}/publish").status_code == 200


def test_resubmitting_identical_subtitles_still_returns_the_delivery(client):
    reel, film = full_setup(client)
    delivery = client.post(f"/films/{film}/deliveries").json()

    same_lines = [
        {"number": 1, "start_ms": 1_000, "end_ms": 2_000, "text": "第一句"},
        {"number": 2, "start_ms": 3_000, "end_ms": 4_000, "text": "第二句"},
        {"number": 3, "start_ms": 5_000, "end_ms": 6_000, "text": "第三句"},
    ]
    put_subtitles(client, reel, same_lines)

    response = client.post(f"/deliveries/{delivery['id']}/publish")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "delivery_stale"


def test_reel_without_subtitles_contributes_nothing(client):
    reel = make_reel(client, "silent")
    film = make_film(client, "f")
    put_edit(client, film, [seg(reel, 0, 1_000, 0)])

    delivery = client.post(f"/films/{film}/deliveries").json()
    assert delivery["sources"] == [
        {"source_reel_id": reel, "source_reel_name": "silent", "subtitle_version": 0}
    ]
    body = client.post(f"/deliveries/{delivery['id']}/publish").json()
    assert body["subtitles"] == []
    assert body["reviews"] == []


def test_first_subtitles_arriving_after_delivery_returns_it(client):
    reel = make_reel(client, "late")
    film = make_film(client, "f")
    put_edit(client, film, [seg(reel, 0, 1_000, 0)])
    delivery = client.post(f"/films/{film}/deliveries").json()

    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 0, "end_ms": 1_000, "text": "迟到的台词"}],
    )

    response = client.post(f"/deliveries/{delivery['id']}/publish")
    assert response.status_code == 409
    assert response.json()["error"]["changes"]["sources"] == [
        {"source_reel_id": reel, "pinned": 0, "current": 1}
    ]


def test_after_rejection_a_new_delivery_can_succeed(client):
    reel, film = full_setup(client)
    first = client.post(f"/films/{film}/deliveries").json()
    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 1_000, "end_ms": 2_000, "text": "新句"}],
    )
    assert client.post(f"/deliveries/{first['id']}/publish").status_code == 409

    second = client.post(f"/films/{film}/deliveries")
    assert second.status_code == 201, second.text
    assert client.post(f"/deliveries/{second.json()['id']}/publish").status_code == 200


def test_latest_publication_returns_most_recent_output_version(client):
    reel, film = full_setup(client)
    first = client.post(f"/films/{film}/deliveries").json()
    client.post(f"/deliveries/{first['id']}/publish")

    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 1_000, "end_ms": 2_000, "text": "第二版"}],
    )
    second = client.post(f"/films/{film}/deliveries").json()
    client.post(f"/deliveries/{second['id']}/publish")

    latest = client.get(f"/films/{film}/subtitles/latest").json()
    assert latest["output_version"] == 2
    assert [c["text"] for c in latest["subtitles"]] == ["第二版"]

    old = client.get(f"/films/{film}/subtitles/latest?output_version=1").json()
    assert old["output_version"] == 1
    assert len(old["subtitles"]) == 3


def test_overlapping_output_segments_are_refused(client):
    reel = make_reel(client)
    film = make_film(client)
    response = client.post(
        f"/films/{film}/edit-tables",
        json={"segments": [seg(reel, 0, 2_000, 0), seg(reel, 2_000, 4_000, 1_000)]},
    )
    assert response.status_code == 422
    assert "overlap" in response.json()["error"]["message"]


def test_edit_table_may_reference_unknown_reel_with_422(client):
    film = make_film(client)
    response = client.post(
        f"/films/{film}/edit-tables", json={"segments": [seg(999, 0, 1_000, 0)]}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_source_reel"


def test_publish_is_idempotent_guard_for_closed_delivery(client):
    _, film = full_setup(client)
    delivery = client.post(f"/films/{film}/deliveries").json()
    assert client.post(f"/deliveries/{delivery['id']}/publish").status_code == 200

    again = client.post(f"/deliveries/{delivery['id']}/publish")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "delivery_not_open"


def test_publishing_frozen_copy_ignores_later_source_changes(client):
    reel, film = full_setup(client)
    delivery = client.post(f"/films/{film}/deliveries").json()
    published = client.post(f"/deliveries/{delivery['id']}/publish").json()

    put_subtitles(
        client,
        reel,
        [{"number": 1, "start_ms": 1_000, "end_ms": 2_000, "text": "之后改的"}],
    )

    frozen = client.get(f"/publications/{published['id']}").json()
    assert [c["text"] for c in frozen["subtitles"]] == ["第一句", "第二句", "第三句"]
    assert frozen["sources"][0]["subtitle_version"] == 1
