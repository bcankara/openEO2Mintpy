"""Unit tests for the openEO client module."""

import json
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

from openeo2mintpy import openeo_client


def test_connect_and_auth():
    with patch("openeo.connect") as mock_connect:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        conn = openeo_client.connect_and_auth("https://fake.openeo.backend")

        mock_connect.assert_called_once_with("https://fake.openeo.backend")
        mock_conn.authenticate_oidc.assert_called_once()
        assert conn == mock_conn


def test_query_burst_acquisitions():
    mock_response_data = {
        "value": [
            {
                "RelativeOrbitNumber": 14,
                "BurstId": 30,
                "BeginningDateTime": "2024-01-01T12:00:00.000Z",
            },
            {
                "RelativeOrbitNumber": 14,
                "BurstId": 30,
                "BeginningDateTime": "2024-01-13T12:00:00.000Z",
            },
        ]
    }
    mock_json_bytes = json.dumps(mock_response_data).encode("utf-8")

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_json_bytes
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        aoi_wkt = "POLYGON((35.30 40.80, 35.30 40.95, 35.50 40.95, 35.50 40.80, 35.30 40.80))"
        bursts = openeo_client.query_burst_acquisitions(
            start_date="2024-01-01",
            end_date="2024-01-31",
            polarisation="VV",
            aoi_wkt=aoi_wkt
        )

        assert len(bursts) == 2
        mock_urlopen.assert_called_once()
        # Verify query formulation
        called_args = mock_urlopen.call_args[0][0]
        # It could be a Request object or a URL string
        if hasattr(called_args, "full_url"):
            called_url = called_args.full_url
        else:
            called_url = called_args
        assert "ContentDate/Start" in urllib.parse.unquote(called_url)


def test_query_burst_acquisitions_error():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = Exception("HTTP Error 500")
        with pytest.raises(RuntimeError, match="Failed to query CDSE catalogue"):
            openeo_client.query_burst_acquisitions(
                start_date="2024-01-01",
                end_date="2024-01-31",
                polarisation="VV",
                aoi_wkt="POLYGON EMPTY"
            )


def test_filter_bursts():
    bursts = [
        {
            "RelativeOrbitNumber": 14,
            "BurstId": 30,
            "SwathIdentifier": "IW1",
            "PlatformSerialIdentifier": "A",
            "BeginningDateTime": "2024-01-01T12:00:00.000Z"
        },
        {
            "RelativeOrbitNumber": 14,
            "BurstId": 30,
            "SwathIdentifier": "IW1",
            "PlatformSerialIdentifier": "C",
            "BeginningDateTime": "2024-01-13T12:00:00.000Z"
        },
        {
            "RelativeOrbitNumber": 14,
            "BurstId": 30,
            "SwathIdentifier": "IW1",
            "PlatformSerialIdentifier": "B",  # Invalid platform B
            "BeginningDateTime": "2024-01-25T12:00:00.000Z"
        },
        {
            "RelativeOrbitNumber": 14,
            "BurstId": 31,  # Different burst id
            "SwathIdentifier": "IW1",
            "PlatformSerialIdentifier": "A",
            "BeginningDateTime": "2024-01-25T12:00:00.000Z"
        },
        {
            "RelativeOrbitNumber": 15,  # Different orbit
            "BurstId": 30,
            "SwathIdentifier": "IW1",
            "PlatformSerialIdentifier": "A",
            "BeginningDateTime": "2024-01-25T12:00:00.000Z"
        },
        {
            "RelativeOrbitNumber": 14,
            "BurstId": 30,
            "SwathIdentifier": "IW2",  # Different sub-swath
            "PlatformSerialIdentifier": "A",
            "BeginningDateTime": "2024-01-25T12:00:00.000Z"
        }
    ]

    dates = openeo_client.filter_bursts(bursts, track=14, burst_id=30, sub_swath="IW1")
    assert dates == ["2024-01-01", "2024-01-13"]


def test_generate_pairs():
    # Dates are sorted
    dates = ["2024-01-01", "2024-01-13", "2024-01-25", "2024-02-06", "2024-03-01"]

    # max_baseline_days = 24
    # Pairs should include sequential (i -> i+1, i -> i+2) if <= 24 days
    # i=0 (01-01): 01-13 (12 days, ok), 01-25 (24 days, ok)
    # i=1 (01-13): 01-25 (12 days, ok), 02-06 (24 days, ok)
    # i=2 (01-25): 02-06 (12 days, ok), 03-01 (36 days, too long)
    # i=3 (02-06): 03-01 (24 days, ok)
    # Total pairs:
    # ["2024-01-01", "2024-01-13"]
    # ["2024-01-01", "2024-01-25"]
    # ["2024-01-13", "2024-01-25"]
    # ["2024-01-13", "2024-02-06"]
    # ["2024-01-25", "2024-02-06"]
    # ["2024-02-06", "2024-03-01"]

    pairs = openeo_client.generate_pairs(dates, max_baseline_days=24)
    expected = [
        ["2024-01-01", "2024-01-13"],
        ["2024-01-01", "2024-01-25"],
        ["2024-01-13", "2024-01-25"],
        ["2024-01-13", "2024-02-06"],
        ["2024-01-25", "2024-02-06"],
        ["2024-02-06", "2024-03-01"]
    ]
    assert pairs == expected


def test_split_pairs_into_groups():
    pairs = [
        ["2024-01-01", "2024-01-13"],
        ["2024-01-01", "2024-01-25"],
        ["2024-01-13", "2024-01-25"],
        ["2024-01-13", "2024-02-06"],
        ["2024-01-25", "2024-02-06"],
        ["2024-02-06", "2024-03-01"]
    ]

    groups = openeo_client.split_pairs_into_groups(pairs)

    # No group should have duplicate primary dates (pair[0])
    for grp in groups:
        primaries = [p[0] for p in grp]
        assert len(primaries) == len(set(primaries))

    # All pairs should be placed
    flat_pairs = [p for grp in groups for p in grp]
    assert len(flat_pairs) == len(pairs)
    for p in pairs:
        assert p in flat_pairs


def test_submit_insar_job():
    mock_conn = MagicMock()
    mock_job = MagicMock()
    mock_job.job_id = "test-job-uuid-1234"

    # We mock StacResource export_workspace and create_job
    with patch("openeo2mintpy.openeo_client.StacResource") as mock_stac_cls:
        mock_stac_instance = MagicMock()
        mock_stac_cls.return_value = mock_stac_instance
        mock_stac_instance.export_workspace.return_value = mock_stac_instance
        mock_stac_instance.create_job.return_value = mock_job

        group_pairs = [["2024-01-01", "2024-01-13"]]

        job_info = openeo_client.submit_insar_job(
            connection=mock_conn,
            track=14,
            direction="ASCENDING",
            burst_id=30,
            sub_swath="IW1",
            group_pairs=group_pairs,
            part_num=1,
            total_parts=2
        )

        assert job_info["job_id"] == "test-job-uuid-1234"
        assert job_info["track"] == 14
        assert job_info["part"] == 1
        assert job_info["pairs_count"] == 1

        mock_stac_cls.assert_called_once()
        mock_stac_instance.export_workspace.assert_called_once()
        mock_stac_instance.create_job.assert_called_once_with(
            title="InSAR_T14_ASCENDING_MintPy_Part1",
            job_options={"python-memory": "4000m"}
        )


def test_download_job_results(tmp_path):
    mock_conn = MagicMock()
    mock_job = MagicMock()
    mock_results = MagicMock()

    mock_conn.job.return_value = mock_job
    mock_job.get_results.return_value = mock_results
    mock_results.download_files.return_value = ["file1.tif", "file2.tif"]

    output_dir = tmp_path / "downloads"

    count = openeo_client.download_job_results(
        connection=mock_conn,
        job_id="test-job-uuid-1234",
        output_dir=output_dir
    )

    assert count == 2
    assert output_dir.is_dir()
    mock_conn.job.assert_called_once_with("test-job-uuid-1234")
    mock_job.get_results.assert_called_once()
    mock_results.download_files.assert_called_once_with(target=str(output_dir))
