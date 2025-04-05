import logging
import os
import uuid
from datetime import datetime, timedelta
from urllib.parse import unquote

import uvicorn
import yt_dlp.version
from fastapi import BackgroundTasks, FastAPI, Response, Body

import config_manager
import download_manager
import process_utils
import programmation_persistence_manager
import ydl_api_ng_utils
from programmation_class import Programmation

try:
    os.mkdir('cookies')
    os.mkdir('logs')
except FileExistsError:
    pass

__cm = config_manager.ConfigManager()

__pu = {}
for queue in __cm.redis_queues:
    __pu[queue] = process_utils.ProcessUtils(__cm, queue_name=queue)

__pm = programmation_persistence_manager.ProgrammationPersistenceManager()

__cm.init_logger(file_name='api.log')

app = FastAPI()
enable_redis = False if __cm.get_app_params().get('_enable_redis') is not True else True

# Middleware function to validate API token
def validate_token(token):
    if not ydl_api_ng_utils.validate_api_token(token):
        return False
    return True

###
# Application
###

@app.get(__cm.get_app_params().get('_api_route_info'))
async def info_request(response: Response, token=None):
    param_token = unquote(token) if token is not None else None
    
    # Check API token first
    if not validate_token(param_token):
        response.status_code = 401
        return {"error": "Invalid or missing API token"}
    
    # Then check user permission if user management is enabled
    user = __cm.is_user_permitted_by_token(param_token)
    if user is False:
        response.status_code = 401
        return {"error": "User not permitted"}

    return {
        'ydl_engine': 'yt-dlp',
        'ydl_version': yt_dlp.version.__version__,
        'ydl_git_head': yt_dlp.version.RELEASE_GIT_HEAD,
        'ydl_api_ng_container_date': os.environ.get('DATE'),
        'ydl_api_ng_branch': os.environ.get('GIT_BRANCH'),
        'ydl_api_ng_revision': os.environ.get('GIT_REVISION'),
        'processed_config': {
            'meta_keys': __cm.get_keys_meta(),
            'app_config': __cm.sanitize_config_object(__cm.get_app_params_object()),
            'user_config': __cm.sanitize_config_object(__cm.get_all_users_params()),
            'preset_config': __cm.sanitize_config_object(__cm.get_all_preset_params()),
            'site_config': __cm.sanitize_config_object(__cm.get_all_sites_params()),
            'auth_config': __cm.sanitize_config_object(__cm.get_all_auth_params()),
            'location_config': __cm.sanitize_config_object(__cm.get_all_locations_params()),
            'template_config': __cm.sanitize_config_object(__cm.get_all_templates_params()),
            'workers_config': __cm.sanitize_config_object(__cm.get_all_workers_params()),
        }
    }


###
# Download
###

@app.get(__cm.get_app_params().get('_api_route_download'))
async def download_request(response: Response, background_tasks: BackgroundTasks, url, token=None, presets=None):
    param_url = unquote(url)
    param_token = unquote(token) if token is not None else None
    param_presets = unquote(presets).split(',') if presets is not None else None

    # Check API token first
    if not validate_token(param_token):
        response.status_code = 401
        return {"error": "Invalid or missing API token"}

    dm = download_manager.DownloadManager(__cm, param_url, param_presets, param_token)

    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return {'status_code': response.status_code}

    if param_url == '':
        response.status_code = 400
        return {'status_code': response.status_code}

    response.status_code = dm.get_api_status_code()

    if response.status_code != 400:
        if enable_redis:
            dm.process_downloads()
        else:
            background_tasks.add_task(dm.process_downloads)

    return dm.get_api_return_object()


@app.post(__cm.get_app_params().get('_api_route_download'))
async def download_request(response: Response, background_tasks: BackgroundTasks, url, body=Body(...), token=None):
    request_id = uuid.uuid4()

    param_url = unquote(url)
    param_token = unquote(token) if token is not None else None

    # Check API token first
    if not validate_token(param_token):
        response.status_code = 401
        return {"error": "Invalid or missing API token"}

    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return {"error": "User not permitted"}

    if param_url == '':
        response.status_code = 400
        return {'status_code': response.status_code}

    if body.get('cookies') is not None:
        cookies_files = open(f'cookies/{request_id}.txt', 'w')
        cookies_files.write(unquote(body.get('cookies')))
        cookies_files.close()

    programmation_object = body.get('programmation')
    generated_programmation = None
    programmation_end_date = None

    if programmation_object is not None:
        programmation_object['url'] = param_url
        programmation_object['user_token'] = token
        programmation_object['not_stored'] = True

        generated_programmation = Programmation(programmation=programmation_object, id=programmation_object.get('id'))

        if len(generated_programmation.errors) != 0:
            response.status_code = 400
            return generated_programmation.errors

        if generated_programmation.recording_duration is not None:
            generated_programmation.recording_stops_at_end = True
            programmation_end_date = datetime.now() + timedelta(minutes=generated_programmation.recording_duration)

    if generated_programmation is None:
        dm = download_manager.DownloadManager(__cm, param_url, None, param_token, body, request_id=request_id)
    else:
        dm = download_manager.DownloadManager(__cm, param_url, None, param_token, body,
                                              programmation_id=generated_programmation.id,
                                              programmation_end_date=programmation_end_date,
                                              programmation_date=datetime.now(),
                                              programmation=generated_programmation.get(),
                                              request_id=request_id)

    response.status_code = dm.get_api_status_code()

    if response.status_code != 400:
        if enable_redis:
            dm.process_downloads()
        else:
            background_tasks.add_task(dm.process_downloads)

    return dm.get_api_return_object()


@app.get(f"{__cm.get_app_params().get('_api_route_download')}/{'{redis_id}'}/failed")
async def relaunch_failed_download(response: Response, redis_id, token=None):
    if not enable_redis:
        response.status_code = 409
        return "Redis management is disabled"

    param_token = unquote(token) if token is not None else None

    # Check API token first
    if not validate_token(param_token):
        response.status_code = 401
        return {"error": "Invalid or missing API token"}

    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return {"error": "User not permitted"}

    for __sub_pu in __pu:
        response.status_code, return_object = __pu.get(__sub_pu).relaunch_failed(redis_id, token)

        if response.status_code != 404:
            return return_object

    return return_object


@app.get(f"{__cm.get_app_params().get('_api_route_download')}/{'{redis_id}'}")
async def relaunch_download(response: Response, redis_id, token=None):
    if not enable_redis:
        response.status_code = 409
        return "Redis management is disabled"

    param_token = unquote(token) if token is not None else None

    # Check API token first
    if not validate_token(param_token):
        response.status_code = 401
        return {"error": "Invalid or missing API token"}

    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return {"error": "User not permitted"}

    for __sub_pu in __pu:
        response.status_code, return_object = __pu.get(__sub_pu).relaunch_job(redis_id, token)

        if response.status_code != 404:
            return return_object

    return return_object


###
# Video
###

@app.get(__cm.get_app_params().get('_api_route_extract_info'))
async def extract_info_request(response: Response, url, token=None):
    param_url = unquote(url)
    param_token = unquote(token) if token is not None else None
    
    # Check API token first
    if not validate_token(param_token):
        response.status_code = 401
        return {"error": "Invalid or missing API token"}
    
    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return {"error": "User not permitted"}

    if param_url == '':
        response.status_code = 400
        return {'status_code': response.status_code}

    info, is_error = download_manager.DownloadManager.extract_info(param_url)

    if is_error:
        response.status_code = 400

    return info


@app.post(__cm.get_app_params().get('_api_route_extract_info'))
async def download_request(response: Response, background_tasks: BackgroundTasks, url, body=Body(...), token=None):
    request_id = uuid.uuid4()

    param_url = unquote(url)
    param_token = unquote(token) if token is not None else None

    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return

    if param_url == '':
        response.status_code = 400
        return {'status_code': response.status_code}

    if body.get('cookies') is not None:
        cookies_files = open(f'cookies/{request_id}.txt', 'w')
        cookies_files.write(unquote(body.get('cookies')))
        cookies_files.close()

    info, is_error = download_manager.DownloadManager.extract_info(param_url, request_id=request_id)

    if is_error:
        response.status_code = 400

    return info


###
# Process
###
@app.get(__cm.get_app_params().get('_api_route_active_downloads'))
async def active_downloads_request(response: Response, token=None, redis_queue=None):
    param_token = unquote(token) if token is not None else None
    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return

    if redis_queue is not None and __pu.get(redis_queue) is not None:
        return __pu.get(redis_queue).get_active_downloads_list()
    elif redis_queue is not None and __pu.get(redis_queue) is None:
        response.status_code = 404
        return f'Redis queue {redis_queue} does not exist'
    else:
        queue_content = {}
        for __sub_pu in __pu:
            queue_content = ydl_api_ng_utils.merge_redis_registries(queue_content,
                                                                    __pu.get(__sub_pu).get_active_downloads_list())

        return queue_content


@app.get(f"{__cm.get_app_params().get('_api_route_active_downloads')}/terminate/{'{pid}'}")
async def terminate_active_download_request(response: Response, background_tasks: BackgroundTasks, pid, token=None,
                                            redis_queue=None):
    param_token = unquote(token) if token is not None else None
    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return

    if redis_queue is not None and __pu.get(redis_queue) is not None:
        return_status = __pu.get(redis_queue).terminate_active_download(unquote(pid), background_tasks=background_tasks)
    elif redis_queue is not None and __pu.get(redis_queue) is None:
        response.status_code = 404
        return f'Redis queue {redis_queue} does not exist'
    else:
        for __sub_pu in __pu:
            return_status = __pu.get(__sub_pu).terminate_active_download(unquote(pid),
                                                                         background_tasks=background_tasks)

            if return_status is not None:
                return return_status

    if return_status is None:
        response.status_code = 404
        return

    return return_status


@app.get(f"{__cm.get_app_params().get('_api_route_active_downloads')}/terminate")
async def terminate_all_active_downloads_request(response: Response, background_tasks: BackgroundTasks, token=None,
                                                 redis_queue=None):
    param_token = unquote(token) if token is not None else None
    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return

    if redis_queue is not None and __pu.get(redis_queue) is not None:
        return __pu.get(redis_queue).terminate_all_active_downloads(background_tasks=background_tasks)
    elif redis_queue is not None and __pu.get(redis_queue) is None:
        response.status_code = 404
        return f'Redis queue {redis_queue} does not exist'
    else:
        terminated_jobs = []
        for __sub_pu in __pu:
            terminated_jobs = terminated_jobs + __pu.get(__sub_pu).terminate_all_active_downloads(
                background_tasks=background_tasks)

        return terminated_jobs


@app.get(f"{__cm.get_app_params().get('_api_route_queue')}")
async def active_downloads_request(response: Response, token=None, redis_queue=None):
    if not enable_redis:
        response.status_code = 409
        return "Redis management is disabled"

    param_token = unquote(token) if token is not None else None
    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return

    if redis_queue is not None and __pu.get(redis_queue) is not None:
        return __pu.get(redis_queue).get_queue_content('all')
    elif redis_queue is not None and __pu.get(redis_queue) is None:
        response.status_code = 404
        return f'Redis queue {redis_queue} does not exist'
    else:
        queue_content = {}
        for __sub_pu in __pu:
            queue_content = ydl_api_ng_utils.merge_redis_registries(queue_content,
                                                                    __pu.get(__sub_pu).get_queue_content('all'))

        return queue_content


@app.delete(f"{__cm.get_app_params().get('_api_route_queue')}")
async def clear_registries(response: Response, token=None, redis_queue=None):
    if not enable_redis:
        response.status_code = 409
        return "Redis management is disabled"

    param_token = unquote(token) if token is not None else None
    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return

    if redis_queue is not None and __pu.get(redis_queue) is not None:
        return __pu.get(redis_queue).clear_all_but_pending_and_started()
    elif redis_queue is not None and __pu.get(redis_queue) is None:
        response.status_code = 404
        return f'Redis queue {redis_queue} does not exist'
    else:
        for __sub_pu in __pu:
            __pu.get(__sub_pu).clear_all_but_pending_and_started()

        return None


@app.put(f"{__cm.get_app_params().get('_api_route_queue')}/{'{pid}'}")
async def update_active_download_download_metadata(response: Response, pid, body=Body(...), token=None):
    if not enable_redis:
        response.status_code = 409
        return "Redis management is disabled"

    param_token = unquote(token) if token is not None else None
    user = __cm.is_user_permitted_by_token(param_token)

    for entry in ['downloaded_files', 'error_files', 'files', 'filename_info']:
        try:
            del body[entry]
            logging.getLogger("api").warning(f'Entry {entry} removed from metadata')
        except KeyError:
            pass

    if user is False:
        response.status_code = 401
        return

    if body.get('programmation_end_date') is not None:
        try:
            datetime.fromisoformat(body.get('programmation_end_date'))
        except Exception as error:
            response.status_code = 400
            return str(error)

    for __sub_pu in __pu:
        updated_job = __pu.get(__sub_pu).update_active_download_metadata(id=unquote(pid), metadata=body)

        if updated_job is not None:
            return updated_job

    response.status_code = 404
    return


@app.get(f"{__cm.get_app_params().get('_api_route_queue')}/{'{registry}'}")
async def active_downloads_request(response: Response, registry, token=None, redis_queue=None):
    if not enable_redis:
        response.status_code = 409
        return "Redis management is disabled"

    param_token = unquote(token) if token is not None else None
    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return

    if redis_queue is not None and __pu.get(redis_queue) is not None:
        return __pu.get(redis_queue).get_queue_content(registry)
    elif redis_queue is not None and __pu.get(redis_queue) is None:
        response.status_code = 404
        return f'Redis queue {redis_queue} does not exist'
    else:
        queue_content = {}
        for __sub_pu in __pu:
            queue_content = ydl_api_ng_utils.merge_redis_registries(queue_content,
                                                                    __pu.get(__sub_pu).get_queue_content(registry))
    return queue_content


###
# Programmation
###

@app.get(f"{__cm.get_app_params().get('_api_route_programmation')}")
async def get_all_active_programmations(response: Response, token=None):
    if not enable_redis:
        response.status_code = 409
        return "Redis management is disabled"

    param_token = unquote(token) if token is not None else None
    user = __cm.is_user_permitted_by_token(param_token)

    if user is False \
            or (user is not None and user.get('_allow_programmation') is None) \
            or (user is not None and user.get('_allow_programmation') is not None and user.get(
        '_allow_programmation') is False):
        response.status_code = 401
        return

    programmations = __pm.get_all_programmations()

    for programmation in programmations:
        programmation['user_token'] = ''

    return programmations


@app.post(f"{__cm.get_app_params().get('_api_route_programmation')}")
async def add_programmation(response: Response, background_tasks: BackgroundTasks, url, override=None, body=Body(...), token=None):
    if not enable_redis:
        response.status_code = 409
        return "Redis management is disabled"

    param_token = unquote(token) if token is not None else None
    user = __cm.is_user_permitted_by_token(param_token)
    param_url = unquote(url)

    if user is False \
            or (user is not None and user.get('_allow_programmation') is None) \
            or (user is not None and user.get('_allow_programmation') is not None and user.get(
        '_allow_programmation') is False):
        response.status_code = 401
        return

    if param_url == '':
        response.status_code = 400
        return {'status_code': response.status_code}

    programmation_object = body
    programmation_object['url'] = param_url
    programmation_object['user_token'] = token

    prog = Programmation(programmation=programmation_object, id=programmation_object.get('id'))

    added = __pm.add_programmation(programmation=prog, override=override=='true')

    if len(prog.errors) != 0:
        response.status_code = 400
        return prog.errors
    else:
        return added.get()


@app.delete(f"{__cm.get_app_params().get('_api_route_programmation')}/{'{id}'}")
async def delete_programmation_by_id(response: Response, id, token=None):
    if not enable_redis:
        response.status_code = 409
        return "Redis management is disabled"

    param_token = unquote(token) if token is not None else None
    user = __cm.is_user_permitted_by_token(param_token)

    if user is False \
            or (user is not None and user.get('_allow_programmation') is None) \
            or (user is not None and user.get('_allow_programmation') is not None and user.get(
        '_allow_programmation') is False):
        response.status_code = 401
        return

    deleted_programmation = __pm.delete_programmation_by_id(id=id)

    if deleted_programmation is not None:
        deleted_programmation['user_token'] = ''
    else:
        response.status_code = 404
        return

    return deleted_programmation


@app.delete(f"{__cm.get_app_params().get('_api_route_programmation')}")
async def delete_programmation_by_url(response: Response, url, token=None):
    if not enable_redis:
        response.status_code = 409
        return "Redis management is disabled"

    param_token = unquote(token) if token is not None else None
    user = __cm.is_user_permitted_by_token(param_token)
    param_url = unquote(url)

    if user is False \
            or (user is not None and user.get('_allow_programmation') is None) \
            or (user is not None and user.get('_allow_programmation') is not None and user.get(
        '_allow_programmation') is False):
        response.status_code = 401
        return

    if param_url == '':
        response.status_code = 400
        return {'status_code': response.status_code}

    deleted_programmations = __pm.delete_programmation_by_url(url=url)

    if deleted_programmations is not None:
        for programmation in deleted_programmations:
            programmation['user_token'] = ''
    else:
        return []

    return deleted_programmations


@app.put(f"{__cm.get_app_params().get('_api_route_programmation')}/{'{id}'}")
async def update_programmation_by_id(response: Response, id, body=Body(...), token=None):
    if not enable_redis:
        response.status_code = 409
        return "Redis management is disabled"

    param_token = unquote(token) if token is not None else None
    user = __cm.is_user_permitted_by_token(param_token)

    if user is False \
            or (user is not None and user.get('_allow_programmation') is None) \
            or (user is not None and user.get('_allow_programmation') is not None and user.get(
        '_allow_programmation') is False):
        response.status_code = 401
        return

    updated_programmation = __pm.update_programmation_by_id(id=id, programmation=body)

    if updated_programmation is None:
        response.status_code = 404
        return

    if len(updated_programmation.errors) != 0:
        response.status_code = 400
        return updated_programmation.errors

    return updated_programmation.get()


###
# File Management
###

@app.delete(__cm.get_app_params().get('_api_route_download') + "/files/{filename:path}")
async def delete_file(response: Response, filename: str = Path(...), token=None):
    param_token = unquote(token) if token is not None else None
    
    # Check API token first
    if not validate_token(param_token):
        response.status_code = 401
        return {"error": "Invalid or missing API token"}
    
    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return {"error": "User not permitted"}

    # Get the absolute path to the downloads folder
    downloads_path = os.path.abspath("./downloads")
    file_path = os.path.abspath(os.path.join("./downloads", filename))
    
    # Verify the file is in the downloads directory (security check)
    if not file_path.startswith(downloads_path):
        response.status_code = 403
        return {"error": "Access denied: Can only delete files in the downloads directory"}
    
    # Check if file exists
    if not os.path.isfile(file_path):
        response.status_code = 404
        return {"error": "File not found"}
    
    try:
        os.remove(file_path)
        return {"status": "success", "file": filename}
    except Exception as e:
        response.status_code = 500
        return {"error": f"Failed to delete file: {str(e)}"}

@app.get(__cm.get_app_params().get('_api_route_download') + "/files")
async def list_files(response: Response, directory: str = "", token=None):
    param_token = unquote(token) if token is not None else None
    
    # Check API token first
    if not validate_token(param_token):
        response.status_code = 401
        return {"error": "Invalid or missing API token"}
    
    user = __cm.is_user_permitted_by_token(param_token)

    if user is False:
        response.status_code = 401
        return {"error": "User not permitted"}

    # Get the absolute path to the downloads folder
    downloads_path = os.path.abspath("./downloads")
    target_dir = os.path.abspath(os.path.join(downloads_path, directory))
    
    # Verify the target directory is within the downloads directory (security check)
    if not target_dir.startswith(downloads_path):
        response.status_code = 403
        return {"error": "Access denied: Can only list files in the downloads directory"}
    
    # Check if directory exists
    if not os.path.isdir(target_dir):
        response.status_code = 404
        return {"error": "Directory not found"}
    
    try:
        files = []
        for root, dirs, filenames in os.walk(target_dir):
            rel_path = os.path.relpath(root, downloads_path)
            rel_path = "" if rel_path == "." else rel_path
            
            for filename in filenames:
                file_path = os.path.join(rel_path, filename)
                abs_path = os.path.join(root, filename)
                file_stats = os.stat(abs_path)
                
                files.append({
                    "name": filename,
                    "path": file_path,
                    "size": file_stats.st_size,
                    "modified": datetime.fromtimestamp(file_stats.st_mtime).isoformat()
                })
                
        return {"files": files}
    except Exception as e:
        response.status_code = 500
        return {"error": f"Failed to list files: {str(e)}"}

if __name__ == '__main__':
    uvicorn.run(app, host=__cm.get_app_params().get('_listen_ip'), port=__cm.get_app_params().get('_listen_port'),
                log_config=None)
