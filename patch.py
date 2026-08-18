import re

with open('kraken_telegram_gateway/gateway/service.py', 'r') as f:
    content = f.read()

old_func_regex = r"def asyncio_run_collect_snapshots\(product_id: str, \*, limit: int, timeout_seconds: float\) -> list\[MarketSnapshot\]:.*?(?=def confirm_trade)"

new_func = """def asyncio_run_collect_snapshots(product_id: str, *, limit: int, timeout_seconds: float) -> list[MarketSnapshot]:
    import asyncio
    import threading

    result = []
    def _thread_worker():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            data = loop.run_until_complete(
                collect_kraken_futures_snapshots(
                    product_id,
                    limit=limit,
                    timeout_seconds=timeout_seconds,
                )
            )
            if data:
                result.extend(data)
        except Exception as e:
            print(f"DEBUG: Error in asyncio thread for {product_id}: {e}")
        finally:
            try:
                loop.close()
            except:
                pass

    t = threading.Thread(target=_thread_worker)
    t.start()
    t.join(timeout=timeout_seconds + 2)
    return result

"""

new_content = re.sub(old_func_regex, new_func, content, flags=re.DOTALL)

with open('kraken_telegram_gateway/gateway/service.py', 'w') as f:
    f.write(new_content)
