"""swarm/urls.py — toda la app es la mesa: el Enjambre vive en la raíz."""
from django.urls import include, path

urlpatterns = [
    # set_language de Django: el botón ES/EN de la navbar postea acá (cookie + redirect).
    path('i18n/', include('django.conf.urls.i18n')),
    path('', include('enjambre.urls')),
]
