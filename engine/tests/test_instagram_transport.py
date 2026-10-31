"""Instagram browser transport, cookie ownership and current query fields."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
from curl_cffi.requests import exceptions as curl_errors

from app.instagram import gallery


class InstagramTransportTests(unittest.TestCase):
    def source(self):
        source = requests.Session()
        source.trust_env = False
        source.cookies.set('sessionid', 'test-only', domain='.instagram.com')
        return source

    def test_transport_shares_the_gallery_cookie_jar_and_proxy_preference(self):
        source = self.source()
        with patch('curl_cffi.requests.Session') as client:
            gallery._BrowserSession(source)
        self.assertIs(client.call_args.kwargs['cookies'], source.cookies)
        self.assertFalse(client.call_args.kwargs['trust_env'])
        self.assertEqual(client.call_args.kwargs['impersonate'], 'chrome136')

    def test_headers_merge_case_insensitively_and_none_removes_a_header(self):
        source = self.source()
        source.headers.update({'X-CSRFToken':'old', 'Cookie':'remove-me'})
        client = Mock()
        with patch('curl_cffi.requests.Session', return_value=client):
            transport = gallery._BrowserSession(source)
        transport.request('POST', 'https://www.instagram.com/api/graphql',
                          headers={'x-csrftoken':'new', 'Cookie':None},
                          timeout=15, verify=True, data={'variables':'{}'})
        sent = client.request.call_args.kwargs
        self.assertEqual(sent['headers']['x-csrftoken'], 'new')
        self.assertNotIn('Cookie', sent['headers'])
        self.assertEqual(source.headers['X-CSRFToken'], 'old')
        self.assertTrue(sent['verify'])
        self.assertEqual(sent['timeout'], 15)

    def test_transport_errors_use_gallerys_existing_http_error_path(self):
        for error, expected in ((curl_errors.Timeout('timed out'), requests.exceptions.Timeout),
                                (curl_errors.RequestException('failed'), requests.exceptions.RequestException)):
            with self.subTest(error=type(error).__name__):
                with patch('curl_cffi.requests.Session', return_value=Mock(request=Mock(side_effect=error))):
                    transport = gallery._BrowserSession(self.source())
                with self.assertRaises(expected):
                    transport.request('GET', 'https://www.instagram.com/')

    def test_instagram_requests_use_one_transport_and_preserve_request_pacing(self):
        source = SimpleNamespace(session=self.source())
        with patch.object(gallery, '_BrowserSession') as transport, \
                patch.object(gallery, '_original_request') as request:
            gallery._checked_request(source, 'https://www.instagram.com/protheme/', interval=False)
            gallery._checked_request(source, 'https://www.instagram.com/api/graphql')
        transport.assert_called_once_with(source.session)
        self.assertIs(request.call_args_list[0].kwargs['session'], transport.return_value)
        self.assertFalse(request.call_args_list[0].kwargs['interval'])
        self.assertEqual(request.call_args_list[0].kwargs['retries'], 0)

    def test_unrelated_hosts_do_not_receive_the_instagram_transport(self):
        for url in ('https://instagram.com.example.org/', 'https://example.com/image.jpg'):
            with self.subTest(url=url):
                with patch.object(gallery, '_BrowserSession') as transport, \
                        patch.object(gallery, '_original_request') as request:
                    gallery._checked_request(SimpleNamespace(), url)
                transport.assert_not_called()
                self.assertNotIn('session', request.call_args.kwargs)

    def test_explicit_session_is_preserved(self):
        session = Mock()
        with patch.object(gallery, '_BrowserSession') as transport, \
                patch.object(gallery, '_original_request') as request:
            gallery._checked_request(SimpleNamespace(), 'https://www.instagram.com/', session=session)
        transport.assert_not_called()
        self.assertIs(request.call_args.kwargs['session'], session)


class InstagramCurrentQueryTests(unittest.TestCase):
    def data(self, operation, variables=None):
        return {'fb_api_req_friendly_name':operation, 'variables':json.dumps(variables or {'id':'123'}),
                'doc_id':'current-query-id', 'lsd':'test-only'}

    def test_profile_query_supplies_the_new_required_banner_field_without_mutating_input(self):
        data = self.data('PolarisProfilePageContentQuery')
        updated = gallery._current_query(data)
        variables = json.loads(updated['variables'])
        self.assertFalse(variables['__relay_internal__pv__PolarisProfileInformTreatmentBannerEnabledrelayprovider'])
        self.assertEqual(json.loads(data['variables']), {'id':'123'})
        self.assertEqual(updated['doc_id'], data['doc_id'])
        self.assertEqual(updated['lsd'], data['lsd'])

    def test_post_query_supplies_crawler_and_comment_fields_and_preserves_cursor(self):
        data = self.data('PolarisProfilePostsTabContentQuery_connection', {'after':'next-page', 'username':'protheme'})
        variables = json.loads(gallery._current_query(data)['variables'])
        self.assertFalse(variables['__relay_internal__pv__PolarisSEOIsCrawlerrelayprovider'])
        self.assertFalse(variables['__relay_internal__pv__PolarisThreadsStyleCommentsEnabledrelayprovider'])
        self.assertEqual(variables['after'], 'next-page')
        self.assertEqual(variables['username'], 'protheme')

    def test_existing_provider_values_and_other_queries_are_preserved(self):
        field = '__relay_internal__pv__PolarisThreadsStyleCommentsEnabledrelayprovider'
        data = self.data('PolarisProfilePostsTabContentQuery_connection', {field:True})
        self.assertTrue(json.loads(gallery._current_query(data)['variables'])[field])
        other = self.data('PolarisProfileStoryHighlightsTrayContentQuery')
        self.assertIs(gallery._current_query(other), other)

    def test_request_updates_the_query_before_gallery_sends_it(self):
        source = SimpleNamespace(_lora1_browser_session=Mock())
        data = self.data('PolarisProfilePageContentQuery')
        with patch.object(gallery, '_original_request') as request:
            gallery._checked_request(source, 'https://www.instagram.com/api/graphql', method='POST', data=data)
        sent = json.loads(request.call_args.kwargs['data']['variables'])
        self.assertIn('__relay_internal__pv__PolarisProfileInformTreatmentBannerEnabledrelayprovider', sent)


if __name__ == '__main__':
    unittest.main()
