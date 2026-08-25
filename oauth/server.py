from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


async def health(request):
    return JSONResponse({
        "status": "ok",
        "service": "ai-autonomous-commerce",
        "environment": "development",
    })


app = Starlette(
    debug=False,
    routes=[
        Route("/health", health, methods=["GET"]),
    ],
)
