"""Protocol tests use a fake Jellyfin server; never modify real library artwork."""
import base64
import sqlite3
import unittest
from io import BytesIO
from unittest.mock import patch
from flask import Flask
from PIL import Image
import image_editor


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.images = [{'ImageType': 'Primary', 'ImageIndex': 0, 'ImageTag': 'a'},
                       {'ImageType': 'Backdrop', 'ImageIndex': 0, 'ImageTag': 'b'},
                       {'ImageType': 'Backdrop', 'ImageIndex': 1, 'ImageTag': 'c'}]
        self.calls = []
        self.scans = []
        self.server = dict(id=1, url='http://jellyfin.invalid', api_key='fake-test-key')
        def connect():
            conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
            conn.execute('CREATE TABLE media_items(server_id, library_id, id, name)')
            conn.execute("INSERT INTO media_items VALUES (1, 'lib', 'item', 'Example')")
            return conn
        app = Flask(__name__)
        image_editor.register_image_editor(app, connect, lambda conn: self.server,
            lambda *args, **kwargs: self.scans.append(kwargs) or 'scan-job')
        self.client = app.test_client()
        self.root = '/fresh/api/libraries/lib/items/item/images'
        self.mock = patch('image_editor.jellyfin_request', side_effect=self.request)
        self.transport = self.mock.start(); self.addCleanup(self.mock.stop)

    def request(self, session, method, url, key, **kwargs):
        self.calls.append((method, url, key, kwargs))
        class Reply:
            ok=True; status_code=200; content=b'image'; headers={'Content-Type':'image/png'}
            def json(reply):
                if url.endswith('/RemoteImages'):
                    return dict(Images=[dict(Type='Primary', Url='https://provider.invalid/poster.png', ProviderName='Jellyfin provider')],TotalRecordCount=150)
                return self.images
        return Reply()

    def edit(self, **body):
        revision = self.client.get(self.root).json['revision']
        return self.client.post(self.root, json=dict(revision=revision, **body))

    def test_all_types_and_private_preview(self):
        data = self.client.get(self.root).json
        self.assertEqual(len(data['types']), 13)
        self.assertNotIn('fake-test-key', str(data))
        self.assertEqual(self.client.get(data['images'][0]['url']).status_code, 200)

    def test_provider_selection_and_pagination(self):
        data=self.client.get(self.root+'/search?type=Primary&start=100').json
        self.assertEqual(data['total'],150)
        self.assertEqual(self.calls[-1][3]['params']['startIndex'],100)
        reply=self.edit(action='select',type='Primary',candidate=data['images'][0]['candidate'])
        self.assertEqual(reply.status_code,200); self.assertEqual(reply.json['job_id'],'scan-job')
        mutation=[c for c in self.calls if c[0]=='POST'][0]
        self.assertTrue(mutation[1].endswith('/RemoteImages/Download'))
        self.assertEqual(mutation[3]['params']['imageUrl'],'https://provider.invalid/poster.png')

    def test_search_language_provider_and_page_controls(self):
        self.client.get(self.root+'/search?type=Primary&start=30&provider=Example&allLanguages=true')
        params=self.calls[-1][3]['params']
        self.assertEqual(params['startIndex'],30)
        self.assertEqual(params['limit'],30)
        self.assertEqual(params['providerName'],'Example')
        self.assertEqual(params['includeAllLanguages'],'true')
        self.client.get(self.root+'/search?type=Primary')
        self.assertEqual(self.calls[-1][3]['params']['includeAllLanguages'],'false')

    def test_tampering_stale_revision_and_wrong_server_rejected(self):
        self.assertEqual(self.edit(action='select',type='Primary',candidate='forged').status_code,400)
        candidate=self.client.get(self.root+'/search?type=Primary').json['images'][0]['candidate']
        self.assertEqual(self.edit(action='select',type='Logo',candidate=candidate).status_code,400)
        self.assertEqual(self.client.post(self.root,json=dict(revision='stale',action='delete',type='Primary',index=0)).status_code,409)
        self.server['id']=2
        self.assertEqual(self.client.get(self.root).status_code,400)
        self.assertFalse(any(c[0]!='GET' for c in self.calls))

    def test_upload_validates_content_and_sends_jellyfin_base64(self):
        buffer=BytesIO();Image.new('RGB',(2,2)).save(buffer,format='PNG')
        encoded=base64.b64encode(buffer.getvalue()).decode()
        self.assertEqual(self.edit(action='upload',type='Logo',data=encoded).status_code,200)
        mutation=[c for c in self.calls if c[0]=='POST'][0]
        self.assertEqual(mutation[3]['data'],encoded.encode())
        self.assertEqual(mutation[3]['headers']['Content-Type'],'image/png')
        self.assertEqual(self.edit(action='upload',type='Logo',data='bad').status_code,400)

    def test_delete_and_reorder(self):
        self.assertEqual(self.edit(action='delete',type='Backdrop',index=1).status_code,200)
        self.assertTrue(any(c[0]=='DELETE' and c[1].endswith('/Images/Backdrop/1') for c in self.calls))
        self.assertEqual(self.edit(action='move',type='Backdrop',index=1,newIndex=0).status_code,200)
        self.assertTrue(any(c[3].get('params')=={'newIndex':0} for c in self.calls))
        self.assertEqual(self.edit(action='move',type='Backdrop',index=1,newIndex=5).status_code,400)
        self.assertEqual(self.edit(action='delete',type='Primary',index=5).status_code,400)
        self.assertEqual(len(self.scans),2)

    def test_invalid_upload_types_rejected_and_backdrop_appends(self):
        buffer=BytesIO();Image.new('RGB',(2,2)).save(buffer,format='PNG')
        encoded=base64.b64encode(buffer.getvalue()).decode()
        for kind in ('Chapter','Screenshot','Profile'):
            self.assertEqual(self.edit(action='upload',type=kind,data=encoded).status_code,400)
        self.assertFalse(any(c[0]!='GET' for c in self.calls))
        self.assertEqual(self.edit(action='upload',type='Backdrop',data=encoded).status_code,200)
        self.assertFalse(any(c[0]=='DELETE' for c in self.calls))

    def test_null_single_image_index_is_normalized(self):
        self.images[0]['ImageIndex']=None
        self.assertEqual(self.client.get(self.root).json['images'][0]['index'],0)
        self.assertEqual(self.edit(action='delete',type='Primary',index=0).status_code,200)
        self.assertTrue(any(c[0]=='DELETE' and c[1].endswith('/Images/Primary/0') for c in self.calls))

    def test_saved_edit_reports_scan_failure_without_false_failure(self):
        # A second image listing failure happens only after the successful write.
        original=self.request
        def failing(session,method,url,key,**kwargs):
            if method=='GET' and self.scans: raise ValueError('Jellyfin unavailable')
            return original(session,method,url,key,**kwargs)
        self.transport.side_effect=failing
        reply=self.edit(action='delete',type='Primary',index=0)
        self.assertEqual(reply.status_code,200);self.assertTrue(reply.json['saved'])
        self.assertIn('warning',reply.json)

if __name__=='__main__': unittest.main()
