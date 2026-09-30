"""Jellyfin-backed image editing; no independent metadata credentials or cache."""
import base64
import hashlib
import json
import secrets
import threading
from io import BytesIO
from urllib.parse import quote

import requests
from flask import request, jsonify, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature
from PIL import Image, UnidentifiedImageError
from generate_html import jellyfin_request

IMAGE_TYPES = ('Primary', 'Art', 'Backdrop', 'Banner', 'Logo', 'Thumb', 'Disc',
               'Box', 'Screenshot', 'Menu', 'Chapter', 'BoxRear', 'Profile')


def register_image_editor(app, connect, active_server, start_scan):
    signer = URLSafeTimedSerializer(secrets.token_hex(32), salt='image-editor')
    lock = threading.Lock()

    def context(library_id, item_id):
        conn = connect()
        try:
            server = active_server(conn)
            item = conn.execute('SELECT name FROM media_items WHERE server_id=? AND library_id=? AND id=?',
                                (server['id'] if server else None, library_id, item_id)).fetchone()
            if not server or not item:
                raise ValueError('Media item not found on the active server. Reopen the listing.')
            return server, item['name']
        finally:
            conn.close()

    def call(server, item_id, suffix, method='GET', **kwargs):
        response = jellyfin_request(requests.Session(), method,
            f"{server['url'].rstrip('/')}/Items/{quote(item_id, safe='')}/{suffix}",
            server['api_key'], timeout=(5, 60), **kwargs)
        if not response.ok:
            raise ValueError(f'Jellyfin rejected the request (HTTP {response.status_code}). Check server permissions and image providers.')
        return response

    def model(server, library_id, item_id):
        values = call(server, item_id, 'Images').json()
        revision = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        images = []
        for value in values:
            kind, index = value['ImageType'], value.get('ImageIndex') or 0
            if kind not in IMAGE_TYPES:
                continue
            images.append(dict(type=kind, index=index, width=value.get('Width'), height=value.get('Height'),
                url=f'/fresh/api/libraries/{quote(library_id)}/items/{quote(item_id)}/images/preview/{kind}/{index}?v={revision}'))
        return dict(images=images, revision=revision, types=IMAGE_TYPES)

    root = '/fresh/api/libraries/<library_id>/items/<item_id>/images'

    @app.route(root, methods=['GET', 'POST'])
    def edit_images(library_id, item_id):
        try:
            server, name = context(library_id, item_id)
            if request.method == 'GET':
                return jsonify(status='ok', name=name, **model(server, library_id, item_id))
            if not lock.acquire(blocking=False):
                return jsonify(status='error', message='Another image edit is in progress. Try again.'), 409
            try:
                body = request.get_json(silent=True) or {}
                if not isinstance(body, dict):
                    raise ValueError('Invalid edit request.')
                current = model(server, library_id, item_id)
                if body.get('revision') != current['revision']:
                    return jsonify(status='error', message='Images changed on Jellyfin. Reload the editor before editing.'), 409
                kind, action = body.get('type'), body.get('action')
                if kind not in IMAGE_TYPES:
                    raise ValueError('Unsupported image type.')
                if action == 'select':
                    try:
                        candidate = signer.loads(body.get('candidate', ''), max_age=3600)
                    except BadSignature:
                        raise ValueError('Search result expired. Search again.') from None
                    if candidate[:3] != [server['id'], item_id, kind]:
                        raise ValueError('Search result belongs to another item or server.')
                    call(server, item_id, 'RemoteImages/Download', 'POST', params={'type': kind, 'imageUrl': candidate[3]})
                elif action == 'upload':
                    if kind in ('Screenshot', 'Profile', 'Chapter'):
                        raise ValueError('This image type cannot be uploaded.')
                    data = body.get('data', '')
                    if not isinstance(data, str) or len(data) > 14_000_000:
                        raise ValueError('Upload must be an image up to 10 MB.')
                    try:
                        raw = base64.b64decode(data, validate=True)
                        if len(raw) > 10 * 1024 * 1024:
                            raise ValueError('Upload must be at most 10 MB.')
                        with Image.open(BytesIO(raw)) as image:
                            mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp', 'GIF': 'image/gif'}.get(image.format)
                            image.verify()
                        if not mime:
                            raise ValueError('Use PNG, JPEG, WebP, or GIF.')
                    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
                        raise ValueError('Invalid image file.') from None
                    call(server, item_id, f'Images/{kind}', 'POST', data=base64.b64encode(raw), headers={'Content-Type': mime})
                elif action in ('delete', 'move'):
                    index = body.get('index')
                    if not any(i['type'] == kind and i['index'] == index for i in current['images']):
                        raise ValueError('Image no longer exists. Reload the editor.')
                    if action == 'delete':
                        call(server, item_id, f'Images/{kind}/{index}', 'DELETE')
                    else:
                        target = body.get('newIndex')
                        if kind != 'Backdrop' or not any(i['type'] == kind and i['index'] == target for i in current['images']):
                            raise ValueError('Invalid backdrop position.')
                        call(server, item_id, f'Images/{kind}/{index}/Index', 'POST', params={'newIndex': target})
                else:
                    raise ValueError('Unsupported image operation.')
                # Jellyfin appends Chapter images like backdrops. Keep only the new
                # chapter image when replacing; upload first so failure preserves originals.
                result = dict(status='ok', saved=True)
                if kind == 'Chapter' and action in ('upload', 'select'):
                    try:
                        for image in sorted((i for i in current['images'] if i['type'] == kind), key=lambda i: i['index'], reverse=True):
                            call(server, item_id, f"Images/{kind}/{image['index']}", 'DELETE')
                    except (ValueError, requests.RequestException):
                        result['warning'] = 'New image saved, but old chapter images could not all be removed. Reload images to review.'
                # Saving succeeded even if a subsequent scan or image reload fails.
                try:
                    result['job_id'] = start_scan('item', server, library_id=library_id, item_id=item_id)
                    result.update(model(server, library_id, item_id))
                except Exception:
                    result['warning'] = 'Image saved. Listing refresh failed; close the editor and use Update to retry.'
                return jsonify(**result)
            finally:
                lock.release()
        except (ValueError, requests.RequestException) as exc:
            return jsonify(status='error', message=str(exc) if isinstance(exc, ValueError) else 'Unable to reach Jellyfin. Try again.'), 400

    @app.route(root + '/search')
    def search_images(library_id, item_id):
        try:
            server, _ = context(library_id, item_id)
            kind = request.args.get('type')
            offset = max(0, int(request.args.get('start', 0)))
            if kind not in IMAGE_TYPES:
                raise ValueError('Unsupported image type.')
            result = call(server, item_id, 'RemoteImages', params={'type': kind, 'startIndex': offset,
                'limit': 30, 'includeAllLanguages': 'true' if request.args.get('allLanguages') == 'true' else 'false',
                'providerName': request.args.get('provider', '')}).json()
            images = [dict(type=kind, url=i['Url'], provider=i.get('ProviderName'), width=i.get('Width'),
                height=i.get('Height'), language=i.get('Language'),
                candidate=signer.dumps([server['id'], item_id, kind, i['Url']]))
                for i in result.get('Images', []) if i.get('Type') == kind]
            return jsonify(status='ok', images=images, total=result.get('TotalRecordCount', len(images)), providers=result.get('Providers', []))
        except (ValueError, requests.RequestException):
            return jsonify(status='error', message='Unable to search Jellyfin image providers.'), 400

    @app.route(root + '/preview/<kind>/<int:index>')
    def preview_image(library_id, item_id, kind, index):
        try:
            server, _ = context(library_id, item_id)
            if kind not in IMAGE_TYPES:
                raise ValueError('Unsupported image type.')
            response = call(server, item_id, f'Images/{kind}/{index}')
            return Response(response.content, mimetype=response.headers.get('Content-Type', 'image/jpeg'),
                            headers={'Cache-Control': 'no-store'})
        except (ValueError, requests.RequestException):
            return Response(status=404)
