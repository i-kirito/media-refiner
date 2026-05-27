#!/usr/bin/env python3
"""Media Refiner - 媒体洗版工坊 入口"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=10308,
        reload=False,
    )
