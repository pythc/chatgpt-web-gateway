from fastapi import HTTPException

from utils.Logger import logger
from utils.configs import retry_times


async def async_retry(func, *args, max_retries=retry_times, **kwargs):
    for attempt in range(max_retries + 1):
        try:
            result = await func(*args, **kwargs)
            return result
        except HTTPException as e:
            if attempt == max_retries:
                logger.error(f"Request failed with HTTP {e.status_code}")
                if e.status_code == 500:
                    raise HTTPException(status_code=500, detail="Server error")
                raise
            logger.info(f"Retry {attempt + 1}, HTTP {e.status_code}")


def retry(func, *args, max_retries=retry_times, **kwargs):
    for attempt in range(max_retries + 1):
        try:
            result = func(*args, **kwargs)
            return result
        except HTTPException as e:
            if attempt == max_retries:
                logger.error(f"Request failed with HTTP {e.status_code}")
                if e.status_code == 500:
                    raise HTTPException(status_code=500, detail="Server error")
                raise
            logger.error(f"Retry {attempt + 1}, HTTP {e.status_code}")
