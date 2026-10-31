"""Instagram Download All must use its own engine and truthful queue results."""
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from gallery_dl import exception
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import models
from app.api.routers import users
from app.core.database import Base
from app.instagram import downloader, gallery
from app.services import download_service, instagram_service, monitor_service


class InstagramBulkTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.user = models.User(state_key='ig:@protheme', platform='instagram', username='protheme', monitored=True)
        self.db.add(self.user)
        self.db.commit()

    def job(self):
        return instagram_service.enqueue_profile_download(self.db, self.user)

    def test_instagram_preview_never_looks_up_a_tiktok_namesake(self):
        with patch.object(monitor_service, 'estimate_all_posts') as tiktok:
            with self.assertRaises(HTTPException) as error:
                users.download_all_preview(self.user.id, self.db)
        self.assertEqual(error.exception.status_code, 409)
        tiktok.assert_not_called()
        self.assertIsNone(self.user.sec_uid)

    def test_internal_catalogue_also_rejects_other_platforms(self):
        with patch.object(monitor_service.listing, 'fetch_all_detailed') as tiktok:
            with self.assertRaises(ValueError):
                monitor_service._fetch_full_catalogue(self.db, self.user)
        tiktok.assert_not_called()

    def test_download_all_queues_one_instagram_profile_without_listing_tiktok(self):
        with patch.object(monitor_service, 'download_all_posts') as tiktok:
            result = users.download_all(self.user.id, self.db)
        self.assertEqual(result.queued, 1)
        tiktok.assert_not_called()
        job = self.db.scalar(select(models.QueueJob))
        self.assertEqual(job.url, 'https://www.instagram.com/protheme/')
        self.assertEqual(job.user_id, self.user.id)

    def test_duplicate_click_reuses_active_profile_job(self):
        first = self.job()
        self.assertEqual(self.job().id, first.id)
        first.status = 'paused'
        self.db.commit()
        self.assertEqual(self.job().id, first.id)
        first.status = 'failed'
        self.db.commit()
        self.assertNotEqual(self.job().id, first.id)

    def test_queue_worker_dispatches_before_using_tiktok_cookies_or_engine(self):
        job = self.job()
        with patch.object(instagram_service, 'process_profile_job', return_value=True) as process, \
                patch.object(download_service.cookie_service, 'active_cookies_path') as tiktok:
            self.assertTrue(download_service.process_job(self.db, job, 'test-worker'))
        process.assert_called_once()
        tiktok.assert_not_called()

    def test_bulk_download_is_uncapped_and_skips_durable_saved_media(self):
        job = self.job()
        with patch.object(instagram_service, 'download_profile', return_value={'ok':True, 'count':2, 'errors':{}}) as download:
            self.assertTrue(instagram_service.process_profile_job(self.db, job, self.user, lambda: None))
        self.assertEqual(download.call_args.kwargs['limit'], 0)
        self.assertTrue(download.call_args.kwargs['use_archive'])
        self.assertEqual(job.status, 'completed')
        self.assertEqual(list(self.db.scalars(select(models.Download))), [])

    def test_rate_limit_and_partial_failures_are_not_reported_as_already_saved(self):
        for count in (0, 2):
            with self.subTest(count=count):
                job = self.job()
                result = {'ok':bool(count), 'count':count, 'errors':{'highlights':'Instagram rate limiting (429)'}}
                with patch.object(instagram_service, 'download_profile', return_value=result), \
                        patch.object(instagram_service.events_service, 'emit'):
                    self.assertFalse(instagram_service.process_profile_job(self.db, job, self.user, lambda: None))
                self.assertEqual(job.status, 'failed')
                self.assertIn('429', job.error)

    def test_pause_and_cancel_keep_queue_controls_and_can_resume(self):
        for state in ('paused', 'cancelled'):
            job = self.job()
            with patch.object(instagram_service, 'download_profile', side_effect=downloader.DownloadStopped(state)):
                self.assertFalse(instagram_service.process_profile_job(self.db, job, self.user, lambda: None))
            self.assertEqual(job.status, state)
            if state == 'paused':
                self.assertIsNone(job.finished_at)
            job.status = 'failed'
            self.db.commit()

    def test_all_three_categories_receive_archive_and_stop_controls(self):
        check = Mock()
        with patch.object(instagram_service.cookie_service, 'has_instagram_cookies', return_value=True), \
                patch.object(instagram_service, 'download_url', return_value={'ok':False,'empty':True}) as download:
            result = instagram_service.download_profile(self.db, 'protheme', limit=0, use_archive=True, check_stop=check)
        self.assertEqual([call.args[1] for call in download.call_args_list],
                         ['https://www.instagram.com/stories/protheme/', 'https://www.instagram.com/protheme/highlights/', 'https://www.instagram.com/protheme/'])
        self.assertTrue(all(call.kwargs['use_archive'] for call in download.call_args_list))
        self.assertTrue(all(call.kwargs['check_stop'] is check for call in download.call_args_list))
        self.assertEqual(download.call_args_list[-1].kwargs['limit'], 0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['count'], 0)

    def test_stopping_stream_kills_the_child_process(self):
        process = Mock(stderr=io.StringIO(''), returncode=0)
        process.poll.return_value = None
        with patch.object(downloader.subprocess, 'Popen', return_value=process), \
                self.assertRaises(downloader.DownloadStopped):
            downloader._run_streaming([], Path('.'), timeout=10, on_item=Mock(),
                                      check_stop=Mock(side_effect=downloader.DownloadStopped('paused')))
        process.kill.assert_called_once()

    def test_success_exit_with_an_extraction_error_is_not_nothing_new(self):
        process = Mock(stderr=io.StringIO('[instagram][error] Request rejected\n'), returncode=0)
        process.poll.return_value = 0
        with patch.object(downloader.subprocess, 'Popen', return_value=process), \
                patch.object(downloader, '_collect', return_value=[]):
            items, error = downloader._run_streaming([], Path('.'), timeout=10, on_item=Mock())
        self.assertEqual(items, [])
        self.assertIn('Request rejected', error)


class InstagramAccessErrorsTests(unittest.TestCase):
    def test_gallery_entrypoint_preserves_the_downloaders_exit_status(self):
        for code in (0, 4):
            with patch.object(gallery, 'install'), patch('gallery_dl.main', return_value=code):
                with self.assertRaises(SystemExit) as stopped:
                    gallery.main()
            self.assertEqual(stopped.exception.code, code)

    def test_removed_search_endpoint_can_fall_back_to_the_profile_page(self):
        api = SimpleNamespace(_strategy_uid=('search','web'),
                              user_by_search=Mock(side_effect=exception.NotFoundError('user')),
                              user_by_web=Mock(return_value={'username':'protheme','id':'123'}), exc=exception)
        self.assertEqual(gallery._user_by_screen_name(api, 'protheme')['username'], 'protheme')
        response = SimpleNamespace(status_code=404, reason='Not Found', url='https://www.instagram.com/web/search/topsearch/')
        with patch.object(gallery, '_original_request', side_effect=exception.HttpError(response=response)):
            with self.assertRaises(exception.HttpError):
                gallery._checked_request(Mock(), response.url)

    def test_http_rate_limit_is_preserved_as_nonzero_extraction_failure(self):
        response = SimpleNamespace(status_code=429, reason='Too Many Requests', url='https://www.instagram.com/')
        with patch.object(gallery, '_original_request', side_effect=exception.HttpError(response=response)):
            with self.assertRaises(exception.AbortExtraction) as error:
                gallery._checked_request(Mock(), 'https://www.instagram.com/')
        self.assertIn('429', str(error.exception))
        self.assertNotEqual(error.exception.code, 0)

    def test_login_redirect_is_not_a_successful_empty_catalogue(self):
        with patch.object(gallery, '_original_request', side_effect=exception.AbortExtraction('HTTP redirect to login page')):
            with self.assertRaises(exception.AbortExtraction) as error:
                gallery._checked_request(Mock(), 'https://www.instagram.com/')
        self.assertNotEqual(error.exception.code, 0)

    def test_user_lookup_does_not_hide_access_failure_as_missing_user(self):
        api = SimpleNamespace(_strategy_uid=('search','web'),
                              user_by_search=Mock(side_effect=exception.AbortExtraction('Instagram rate limiting (429)')),
                              user_by_web=Mock(), exc=exception)
        with self.assertRaises(exception.AbortExtraction):
            gallery._user_by_screen_name(api, 'protheme')
        api.user_by_web.assert_not_called()
