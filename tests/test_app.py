import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
import app


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = app.DATA
        app.DATA = Path(self.temp.name)
        app.init()

    def tearDown(self):
        app.DATA = self.old_data
        self.temp.cleanup()

    def test_seed_once_and_delete_persists(self):
        with app.db() as con:
            self.assertEqual(con.execute('SELECT name FROM keywords ORDER BY id').fetchall()[0][0], '삼성전자')
            con.execute('DELETE FROM keywords')
        app.init()
        with app.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM keywords').fetchone()[0], 0)

    def test_collect_dedup_keyword_matching_and_failure_isolation(self):
        row = dict(title='삼성전자 하이닉스 반도체', summary='뉴스', url='https://example.com/news/1', publisher='일보', published=app.now())
        def fake(provider, keyword):
            if provider == 'daum':
                raise TimeoutError()
            return [row, row, dict(row, title='관련 없는 기사', summary='무관'), dict(row, url='javascript:alert(1)')]
        with patch.object(app, 'fetch', side_effect=fake):
            for _ in range(2):
                app.LOCK.acquire()
                app.collect_locked()
        with app.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM articles').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM matches').fetchone()[0], 4)
            self.assertEqual(con.execute("SELECT count(*) FROM runs WHERE status='error'").fetchone()[0], 2)
        self.assertFalse(app.LOCK.locked())

    def test_google_xml_parsing(self):
        xml = '<rss><channel><item><title>삼성전자 &amp; 하이닉스</title><link>https://example.com/a</link><description>&lt;b&gt;요약&lt;/b&gt;</description><source>일보</source><pubDate>Tue, 03 Nov 2026 00:00:00 GMT</pubDate></item></channel></rss>'.encode()
        with patch.object(app, 'request', return_value=xml):
            result = app.fetch('google', '삼성전자')
        self.assertEqual(result[0]['summary'], '요약')
        self.assertEqual(result[0]['published'], '2026-11-03T00:00:00+00:00')

    def test_http_validation_and_static(self):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with urllib.request.urlopen(base + '/api/state') as response:
                self.assertEqual(len(json.load(response)['keywords']), 2)
            with urllib.request.urlopen(base + '/') as response:
                self.assertIn('NEWS RADAR', response.read().decode())
            request = urllib.request.Request(base + '/api/keywords', data=json.dumps({'name':'테스트'}).encode(), headers={'Content-Type':'application/json','X-News-Radar':'1'})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 201)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 409)
            request = urllib.request.Request(base + '/api/collect', data=b'{}')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_naver_expands_search_and_ignores_api_keys(self):
        empty = b'<rss><channel /></rss>'
        unrelated = '<rss><channel><item><title>무관</title><link>https://example.com/a</link></item></channel></rss>'.encode()
        found = '<rss><channel><item><title>삼성전자 기사</title><link>https://example.com/a</link></item></channel></rss>'.encode()
        with patch.dict('os.environ', {'NAVER_CLIENT_ID':'unused','NAVER_CLIENT_SECRET':'unused'}):
            with patch.object(app, 'request', side_effect=[empty, unrelated, found]) as mock:
                rows = app.fetch('naver', '삼성전자')
        queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(c.args[0]).query)['q'][0] for c in mock.call_args_list]
        self.assertEqual(queries, ['삼성전자 site:news.naver.com when:7d', '삼성전자 site:news.naver.com when:30d', '삼성전자 site:news.naver.com'])
        self.assertIn('기간 제한 없음', rows[0]['method'])
        self.assertEqual(rows[0]['published'], '')
        self.assertTrue(all(urllib.parse.urlsplit(c.args[0]).hostname == 'news.google.com' and len(c.args)==1 for c in mock.call_args_list))

    def test_naver_stops_after_first_match(self):
        found = '<rss><channel><item><title>하이닉스 기사</title><link>https://example.com/a</link></item></channel></rss>'.encode()
        with patch.object(app, 'request', return_value=found) as mock:
            rows = app.fetch('naver', '하이닉스')
        self.assertEqual(mock.call_count, 1)
        self.assertTrue(rows[0]['method'].endswith('7일'))

    def test_empty_result_and_invalid_feed(self):
        with patch.object(app, 'request', side_effect=[b'<rss><channel /></rss>'] * 3 + ['<html>검색결과가 없습니다</html>'.encode()]) as mock:
            self.assertEqual(app.fetch('naver', '삼성전자'), [])
        self.assertEqual(mock.call_count, 4)
        with patch.object(app, 'request', return_value=b'<html><body>Blocked</body></html>'):
            with self.assertRaises(ValueError):
                app.fetch('naver', '삼성전자')

    def test_daum_ignores_api_key(self):
        with patch.dict('os.environ', {'KAKAO_REST_API_KEY':'unused'}):
            with patch.object(app, 'request', return_value=b'<rss><channel /></rss>') as mock:
                self.assertEqual(app.fetch('daum', '삼성전자'), [])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(mock.call_args.args[0]).query)['q'][0]
        self.assertEqual(query, '"삼성전자" site:v.daum.net when:7d')
        self.assertEqual(len(mock.call_args.args), 1)

    def test_direct_naver_fallback_parses_article_anchors(self):
        body = '<html><a href="https://example.com/1" data-heatmap-target=".tit"><span>반도체 소식</span>새 창 열기</a><a href="https://example.com/1" data-heatmap-target=".body">삼성전자 실적</a><a href="javascript:alert(1)" data-heatmap-target=".tit">삼성전자</a></html>'.encode()
        with patch.object(app, 'request', side_effect=[b'<rss><channel /></rss>'] * 3 + [body]) as mock:
            rows = app.fetch('naver', '삼성전자')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], '반도체 소식')
        self.assertEqual(rows[0]['summary'], '삼성전자 실적')
        self.assertEqual(rows[0]['published'], '')
        self.assertIn('직접 수집', rows[0]['method'])
        self.assertEqual(urllib.parse.urlsplit(mock.call_args.args[0]).hostname, 'search.naver.com')
        with patch.object(app, 'request', return_value=b'<html>Challenge</html>'):
            with self.assertRaises(ValueError):
                app.fetch_naver_page('삼성전자')


if __name__ == '__main__':
    unittest.main()
