from django.urls import path

from . import views

app_name = 'enjambre'

urlpatterns = [
    path('', views.home, name='home'),
    path('sesion/nueva/', views.crear_sesion, name='crear_sesion'),
    path('sesion/<int:pk>/', views.mesa, name='mesa'),
    path('sesion/<int:pk>/borrar/', views.borrar_sesion, name='borrar_sesion'),
    path('sesion/<int:pk>/config/', views.guardar_config, name='guardar_config'),
    path('sesion/<int:pk>/renombrar/', views.renombrar_sesion, name='renombrar_sesion'),
    path('sesion/<int:pk>/fijar/', views.fijar_sesion, name='fijar_sesion'),
    path('sesion/<int:pk>/preguntar/', views.preguntar, name='preguntar'),
    # DORMIDA: fabricar es ahora el comando «/armar» en la mesa. crear_tarea (modo-repo) queda
    # parqueada para la fase Líder; ningún template la usa. Se mantiene la ruta para no romper refs.
    path('sesion/<int:pk>/tarea/', views.crear_tarea, name='crear_tarea'),
    path('sesion/<int:pk>/stream/', views.stream, name='stream'),
    path('sesion/<int:pk>/logs/', views.log_stream, name='log_stream'),
    path('sesion/<int:pk>/archivos/', views.mesa_archivos, name='mesa_archivos'),
    path('sesion/<int:pk>/archivo/', views.mesa_archivo, name='mesa_archivo'),
    path('sesion/<int:pk>/zip/', views.mesa_zip, name='mesa_zip'),
    path('sesion/<int:pk>/subir/', views.mesa_subir, name='mesa_subir'),
    path('sesion/<int:pk>/bitacora/', views.bitacora, name='bitacora'),
    path('sesion/<int:pk>/informe/', views.informe, name='informe'),
    path('accion/<int:accion_id>/', views.accion_resolver, name='accion_resolver'),
    path('worker/restart/', views.worker_restart, name='worker_restart'),
    path('ayuda/', views.ayuda, name='ayuda'),
    path('conexiones/', views.conexiones, name='conexiones'),
    path('conexiones/vault/', views.vault_keys, name='vault_keys'),
    path('conexiones/modelos/', views.modelos_disponibles, name='modelos_disponibles'),
    path('sillas/', views.gestionar_sillas, name='gestionar_sillas'),
    path('sillas/avatares/', views.guardar_avatares, name='guardar_avatares'),
    path('sillas/nueva/', views.crear_silla, name='crear_silla'),
    path('sillas/<slug:key>/guardar/', views.guardar_silla, name='guardar_silla'),
    path('sillas/<slug:key>/clonar/', views.clonar_silla, name='clonar_silla'),
    path('sillas/<slug:key>/borrar/', views.borrar_silla, name='borrar_silla'),
    path('sillas/<slug:key>/persona/', views.guardar_persona, name='guardar_persona'),
]
