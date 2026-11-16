using System.Reflection;
using System.Runtime.Loader;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Plugins;
using MediaBrowser.Controller.Security;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Newtonsoft.Json.Linq;

namespace Jellyfin.Plugin.Share;

public class PluginServiceRegistrator : IPluginServiceRegistrator
{
    public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
    {
        serviceCollection.AddSingleton<ShareStore>();
        serviceCollection.AddSingleton<ShareApiKey>();
        serviceCollection.AddHttpClient(ShareApiKey.HttpClientName, c => c.Timeout = Timeout.InfiniteTimeSpan);
        serviceCollection.AddHostedService<ShareStartup>();
    }
}

/// <summary>API key interna (nunca sai do servidor) usada para pedir os streams HLS ao próprio Jellyfin.</summary>
public class ShareApiKey
{
    public const string AppName = "Compartilhar (plugin)";
    public const string HttpClientName = "JellyfinShare";
    private readonly IAuthenticationManager _auth;
    private readonly SemaphoreSlim _sem = new(1, 1);
    private string? _key;

    public ShareApiKey(IAuthenticationManager auth) => _auth = auth;

    public async Task<string> GetAsync()
    {
        if (_key is not null)
        {
            return _key;
        }

        await _sem.WaitAsync().ConfigureAwait(false);
        try
        {
            var existing = (await _auth.GetApiKeys().ConfigureAwait(false)).FirstOrDefault(k => k.AppName == AppName);
            if (existing is null)
            {
                await _auth.CreateApiKey(AppName).ConfigureAwait(false);
                existing = (await _auth.GetApiKeys().ConfigureAwait(false)).First(k => k.AppName == AppName);
            }

            _key = existing.AccessToken;
            return _key;
        }
        finally
        {
            _sem.Release();
        }
    }
}

/// <summary>Callback do File Transformation: injeta o client.js no index.html do jellyfin-web.</summary>
public class IndexHtmlPatch
{
    public string Contents { get; set; } = string.Empty;
}

public static class IndexHtmlTransformer
{
    public const string Marker = "<!-- jellyfin-share -->";

    public static string Transform(IndexHtmlPatch payload)
    {
        var html = payload.Contents ?? string.Empty;
        if (html.Contains(Marker, StringComparison.Ordinal))
        {
            return html;
        }

        var v = typeof(IndexHtmlTransformer).Assembly.GetName().Version?.ToString() ?? "1";
        var tag = $"{Marker}<script defer src=\"../Share/client.js?v={v}\"></script>";
        var i = html.LastIndexOf("</body>", StringComparison.OrdinalIgnoreCase);
        return i >= 0 ? html.Insert(i, tag) : html + tag;
    }
}

public class ShareStartup : IHostedService
{
    private static readonly Guid TransformationId = new("c5a0b3e2-7f14-4d26-8e9b-31d4f6a2c7d8");
    private readonly ILogger<ShareStartup> _logger;
    private readonly ShareApiKey _apiKey;

    public ShareStartup(ILogger<ShareStartup> logger, ShareApiKey apiKey)
    {
        _logger = logger;
        _apiKey = apiKey;
    }

    public Task StartAsync(CancellationToken cancellationToken)
    {
        _ = Task.Run(RegisterAsync, CancellationToken.None);
        return Task.CompletedTask;
    }

    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    private async Task RegisterAsync()
    {
        for (var attempt = 1; attempt <= 30; attempt++)
        {
            try
            {
                var ft = AssemblyLoadContext.All.SelectMany(x => x.Assemblies)
                    .FirstOrDefault(a => a.GetName().Name == "Jellyfin.Plugin.FileTransformation");
                if (ft is null)
                {
                    _logger.LogWarning("[Share] Plugin File Transformation não está instalado: o botão Compartilhar não aparecerá no jellyfin-web.");
                    break;
                }

                var method = ft.GetType("Jellyfin.Plugin.FileTransformation.PluginInterface")
                    ?.GetMethod("RegisterTransformation", BindingFlags.Public | BindingFlags.Static)
                    ?? throw new InvalidOperationException("PluginInterface.RegisterTransformation não encontrado");
                var payload = new JObject
                {
                    ["id"] = TransformationId.ToString(),
                    ["fileNamePattern"] = "index.html",
                    ["callbackAssembly"] = typeof(IndexHtmlTransformer).Assembly.FullName,
                    ["callbackClass"] = typeof(IndexHtmlTransformer).FullName,
                    ["callbackMethod"] = nameof(IndexHtmlTransformer.Transform),
                };
                var pType = method.GetParameters()[0].ParameterType;
                var arg = pType.IsInstanceOfType(payload)
                    ? payload
                    : pType.GetMethod("Parse", new[] { typeof(string) })!.Invoke(null, new object[] { payload.ToString() });
                method.Invoke(null, new[] { arg });
                _logger.LogInformation("[Share] client.js registrado no File Transformation.");
                break;
            }
            catch (Exception ex)
            {
                if (attempt == 30)
                {
                    _logger.LogError(ex, "[Share] Não consegui registrar no File Transformation.");
                }

                await Task.Delay(TimeSpan.FromSeconds(2)).ConfigureAwait(false);
            }
        }

        try
        {
            await _apiKey.GetAsync().ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[Share] Falha criando a API key interna.");
        }
    }
}
