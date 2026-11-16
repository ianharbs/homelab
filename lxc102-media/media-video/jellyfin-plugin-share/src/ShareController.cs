using System.IO.Compression;
using System.Net;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Jellyfin.Api.Extensions;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Entities;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.Features;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Share;

[ApiController]
[Route("Share")]
public partial class ShareController : ControllerBase
{
    private static readonly JsonSerializerOptions Camel = new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };

    // Caminhos HLS que o Jellyfin devolve nas playlists (relativos a /Videos/{id}/).
    [GeneratedRegex(@"^(master\.m3u8|main\.m3u8|hls1/[A-Za-z0-9_]+/-?\d+\.(mp4|ts|m4s|aac))$")]
    private static partial Regex HlsPathRegex();

    private static readonly Dictionary<string, (int Bitrate, int? MaxWidth)> Qualities = new()
    {
        ["original"] = (60_000_000, null),
        ["1080"] = (8_000_000, 1920),
        ["720"] = (4_000_000, 1280),
        ["480"] = (1_500_000, 854),
    };

    private static readonly HashSet<string> ForwardedParams = new(StringComparer.OrdinalIgnoreCase)
    {
        "PlaySessionId", "VideoCodec", "AudioCodec", "VideoBitrate", "AudioBitrate", "TranscodingMaxAudioChannels",
        "SegmentContainer", "MinSegments", "BreakOnNonKeyFrames", "AllowVideoStreamCopy", "AllowAudioStreamCopy",
        "h264-profile", "h264-level", "MaxWidth", "AudioStreamIndex", "SubtitleStreamIndex", "SubtitleMethod",
        "runtimeTicks", "actualSegmentLengthTicks",
    };

    // Sessões de reprodução ativas por link (limita quantas transcodificações um link pode abrir).
    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, System.Collections.Concurrent.ConcurrentDictionary<string, DateTime>> Sessions = new();

    private readonly ShareStore _store;
    private readonly ShareApiKey _apiKey;
    private readonly ILibraryManager _library;
    private readonly IMediaSourceManager _mediaSources;
    private readonly IUserManager _users;
    private readonly IServerApplicationHost _host;
    private readonly IHttpClientFactory _http;
    private readonly ILogger<ShareController> _logger;

    public ShareController(
        ShareStore store,
        ShareApiKey apiKey,
        ILibraryManager library,
        IMediaSourceManager mediaSources,
        IUserManager users,
        IServerApplicationHost host,
        IHttpClientFactory http,
        ILogger<ShareController> logger)
    {
        _store = store;
        _apiKey = apiKey;
        _library = library;
        _mediaSources = mediaSources;
        _users = users;
        _host = host;
        _http = http;
        _logger = logger;
    }

    private static Configuration.PluginConfiguration Config => Plugin.Instance!.Configuration;

    // ------------------------------------------------------------------ autenticado (jellyfin-web)

    [HttpGet("client.js")]
    [AllowAnonymous]
    public IActionResult ClientJs()
    {
        Response.Headers.CacheControl = "public, max-age=3600";
        return Content(Resource("client.js"), "application/javascript; charset=utf-8");
    }

    [HttpGet("hls.min.js")]
    [AllowAnonymous]
    public IActionResult HlsJs()
    {
        Response.Headers.CacheControl = "public, max-age=604800";
        return Content(Resource("hls.min.js"), "application/javascript; charset=utf-8");
    }

    [HttpGet("Config")]
    [Authorize]
    public IActionResult GetClientConfig()
    {
        return Json(new { canShare = CanShare(), defaultDays = Config.DefaultDays });
    }

    public class CreateRequest
    {
        public Guid ItemId { get; set; }

        public int Days { get; set; }
    }

    [HttpPost("Links")]
    [Authorize]
    public IActionResult CreateLink([FromBody] CreateRequest req)
    {
        if (!CanShare())
        {
            return Forbid();
        }

        // API key (automação): age como o primeiro administrador
        var user = User.GetIsApiKey()
            ? _users.GetUsers().FirstOrDefault(u => u.Permissions.Any(p => p.Kind == Jellyfin.Database.Implementations.Enums.PermissionKind.IsAdministrator && p.Value))
            : _users.GetUserById(User.GetUserId());
        var item = _library.GetItemById(req.ItemId);
        if (user is null || item is null || !item.IsVisible(user))
        {
            return NotFound();
        }

        if (item is not (Series or Season or Episode or Movie))
        {
            return BadRequest("Só dá para compartilhar séries, temporadas, episódios e filmes.");
        }

        var days = Math.Clamp(req.Days, 0, 3650);
        var link = _store.Create(item.Id, item.GetType().Name, DisplayName(item), user.Id, user.Username, days);
        _logger.LogInformation("[Share] {User} criou link {Id} para {Item} (validade: {Days})", user.Username, link.Id, link.ItemName, days == 0 ? "sem" : days + " dias");
        return Json(LinkDto(link));
    }

    [HttpGet("Links")]
    [Authorize(Policy = "RequiresElevation")]
    public IActionResult ListLinks()
    {
        return Json(_store.All().Select(LinkDto));
    }

    [HttpDelete("Links/{id}")]
    [Authorize(Policy = "RequiresElevation")]
    public IActionResult RevokeLink(string id)
    {
        return _store.Revoke(id) ? NoContent() : NotFound();
    }

    [HttpPost("Links/RemoveExpired")]
    [Authorize(Policy = "RequiresElevation")]
    public IActionResult RemoveExpired()
    {
        return Json(new { removed = _store.RemoveExpired() });
    }

    // ------------------------------------------------------------------ público (quem recebe o link)

    [HttpGet("v/{token}")]
    [AllowAnonymous]
    public IActionResult Page(string token)
    {
        Response.Headers["X-Robots-Tag"] = "noindex, nofollow";
        Response.Headers["Referrer-Policy"] = "no-referrer";
        Response.Headers["X-Content-Type-Options"] = "nosniff";
        Response.Headers["X-Frame-Options"] = "DENY";
        Response.Headers["Content-Security-Policy"] =
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; " +
            "media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'";
        var link = _store.GetValid(token);
        var item = link is null ? null : _library.GetItemById(link.ItemId);
        if (link is null || item is null)
        {
            Response.StatusCode = 410;
            return Content(Resource("gone.html"), "text/html; charset=utf-8");
        }

        var pub = PublicBase();
        var imgItem = item is Episode or Season && !item.HasImage(ImageType.Primary) ? SeriesOf(item) ?? item : item;
        var html = Resource("share.html")
            .Replace("{{API}}", Request.PathBase + "/Share/v/" + Uri.EscapeDataString(token), StringComparison.Ordinal)
            .Replace("{{BASE}}", Request.PathBase.ToString(), StringComparison.Ordinal)
            .Replace("{{TITLE}}", WebUtility.HtmlEncode(link.ItemName), StringComparison.Ordinal)
            .Replace("{{DESC}}", WebUtility.HtmlEncode(Truncate(item.Overview ?? string.Empty, 200)), StringComparison.Ordinal)
            .Replace("{{OGIMAGE}}", WebUtility.HtmlEncode($"{pub}/Items/{imgItem.Id:N}/Images/Primary?maxWidth=600"), StringComparison.Ordinal);
        return Content(html, "text/html; charset=utf-8");
    }

    [HttpGet("v/{token}/info")]
    [AllowAnonymous]
    public IActionResult Info(string token)
    {
        var link = _store.GetValid(token);
        var item = link is null ? null : _library.GetItemById(link.ItemId);
        if (link is null || item is null)
        {
            return StatusCode(410);
        }

        _store.RegisterView(link);
        var series = SeriesOf(item);
        var episodes = Videos(link, item);
        var seasons = episodes
            .GroupBy(v => v is Episode e ? e.SeasonId : Guid.Empty)
            .Select(g =>
            {
                var first = g.First() as Episode;
                var idx = first?.ParentIndexNumber;
                return new
                {
                    id = g.Key == Guid.Empty ? item.Id : g.Key,
                    name = first is null ? item.Name : (first.SeasonName ?? (idx == 0 ? "Especiais" : $"Temporada {idx}")),
                    index = idx,
                    episodes = g.Select(VideoDto).ToList(),
                };
            })
            .OrderBy(s => s.index is null or 0 ? int.MaxValue : s.index)
            .ToList();

        var posterItem = item.HasImage(ImageType.Primary) && item is not Episode ? item : series ?? item;
        var backdropItem = (series ?? item).HasImage(ImageType.Backdrop) ? series ?? item : null;
        var logoItem = (series ?? item).HasImage(ImageType.Logo) ? series ?? item : null;
        return Json(new
        {
            kind = item.GetType().Name.ToLowerInvariant(),
            rootId = item.Id.ToString("N"),
            title = series?.Name ?? item.Name,
            subtitle = item switch
            {
                Season s => s.Name,
                Episode e => $"{EpCode(e)} · {e.Name}",
                _ => null,
            },
            overview = item.Overview ?? series?.Overview,
            year = (series ?? item).ProductionYear,
            posterId = posterItem.Id.ToString("N"),
            backdropId = backdropItem?.Id.ToString("N"),
            logoId = logoItem?.Id.ToString("N"),
            expiresAt = link.ExpiresAt,
            total = episodes.Count,
            seasons,
        });
    }

    [HttpGet("v/{token}/media/{itemId}")]
    [AllowAnonymous]
    public IActionResult Media(string token, Guid itemId)
    {
        if (!TryGetVideo(token, itemId, out _, out var video))
        {
            return StatusCode(410);
        }

        var streams = _mediaSources.GetMediaStreams(video.Id);
        var v = streams.FirstOrDefault(s => s.Type == MediaStreamType.Video);
        long? size = null;
        try
        {
            size = System.IO.File.Exists(video.Path) ? new FileInfo(video.Path).Length : null;
        }
        catch (IOException)
        {
        }

        return Json(new
        {
            id = video.Id.ToString("N"),
            fileName = Path.GetFileName(video.Path),
            size,
            runtime = video.RunTimeTicks / TimeSpan.TicksPerSecond,
            video = v is null ? null : new { codec = v.Codec, width = v.Width, height = v.Height, range = v.VideoRangeType.ToString() },
            audio = streams.Where(s => s.Type == MediaStreamType.Audio).Select(s => new
            {
                index = s.Index,
                label = s.DisplayTitle ?? s.Language ?? $"Faixa {s.Index}",
                lang = s.Language,
                isDefault = s.IsDefault,
            }),
            subs = streams.Where(s => s.Type == MediaStreamType.Subtitle).Select(s => new
            {
                index = s.Index,
                label = s.DisplayTitle ?? s.Language ?? $"Legenda {s.Index}",
                lang = s.Language,
                text = s.IsTextSubtitleStream,
                forced = s.IsForced,
                isDefault = s.IsDefault,
            }),
        });
    }

    /// <summary>Playlist mestre: os parâmetros de transcodificação são montados aqui, não vêm do cliente.</summary>
    [HttpGet("v/{token}/hls/{itemId}/master.m3u8")]
    [AllowAnonymous]
    public async Task Master(string token, Guid itemId, [FromQuery] string? q, [FromQuery] int? audio, [FromQuery] int? burnSub, [FromQuery] string? ps)
    {
        if (!TryGetVideo(token, itemId, out var link, out var video))
        {
            Response.StatusCode = 410;
            return;
        }

        var playSession = SafeId(ps) ?? Guid.NewGuid().ToString("N");
        if (!TouchSession(link, playSession, isNew: true))
        {
            Response.StatusCode = 429;
            await Response.WriteAsync("Muitas reproduções simultâneas neste link.").ConfigureAwait(false);
            return;
        }

        var (bitrate, maxWidth) = Qualities.GetValueOrDefault(q ?? "original", Qualities["original"]);
        var id = video.Id.ToString("N");
        var qs = new List<string>
        {
            "MediaSourceId=" + id,
            "DeviceId=" + DeviceId(link),
            "PlaySessionId=" + playSession,
            "VideoCodec=h264",
            "AudioCodec=aac",
            "VideoBitrate=" + bitrate,
            "AudioBitrate=192000",
            "TranscodingMaxAudioChannels=2",
            "SegmentContainer=mp4",
            "MinSegments=1",
            "BreakOnNonKeyFrames=true",
            "AllowVideoStreamCopy=true",
            "AllowAudioStreamCopy=true",
            "h264-profile=high,main,baseline,constrained baseline".Replace(" ", "%20", StringComparison.Ordinal),
            "h264-level=51",
        };
        if (maxWidth is int w)
        {
            qs.Add("MaxWidth=" + w);
        }

        if (audio is int a)
        {
            qs.Add("AudioStreamIndex=" + a);
        }

        if (burnSub is int s && s >= 0)
        {
            qs.Add("SubtitleStreamIndex=" + s);
            qs.Add("SubtitleMethod=Encode");
        }

        _store.Touch(link);
        await ProxyAsync(link, $"/Videos/{id}/master.m3u8?{string.Join('&', qs)}", isPlaylist: true).ConfigureAwait(false);
    }

    /// <summary>main.m3u8 e segmentos: repassa ao Jellyfin com a API key interna (só para itens deste link).</summary>
    [HttpGet("v/{token}/hls/{itemId}/{**path}")]
    [AllowAnonymous]
    public async Task Hls(string token, Guid itemId, string path)
    {
        if (!HlsPathRegex().IsMatch(path) || !TryGetVideo(token, itemId, out var link, out var video))
        {
            Response.StatusCode = 404;
            return;
        }

        // Só repassa os parâmetros que o próprio master.m3u8 gerou; MediaSourceId e DeviceId são forçados.
        var psId = SafeId(Request.Query["PlaySessionId"].FirstOrDefault());
        if (psId is null)
        {
            Response.StatusCode = 400;
            return;
        }

        TouchSession(link, psId);
        var parts = Request.Query
            .Where(kv => ForwardedParams.Contains(kv.Key))
            .SelectMany(kv => kv.Value.Take(1).Select(v => Uri.EscapeDataString(kv.Key) + "=" + Uri.EscapeDataString(v ?? string.Empty)))
            .Append("MediaSourceId=" + video.Id.ToString("N"))
            .Append("DeviceId=" + DeviceId(link));
        await ProxyAsync(link, $"/Videos/{video.Id:N}/{path}?{string.Join('&', parts)}", isPlaylist: path.EndsWith(".m3u8", StringComparison.Ordinal)).ConfigureAwait(false);
    }

    [HttpGet("v/{token}/sub/{itemId}/{index}.vtt")]
    [AllowAnonymous]
    public async Task Subtitle(string token, Guid itemId, int index)
    {
        if (!TryGetVideo(token, itemId, out var link, out var video))
        {
            Response.StatusCode = 410;
            return;
        }

        await ProxyAsync(link, $"/Videos/{video.Id:N}/{video.Id:N}/Subtitles/{index}/0/Stream.vtt", isPlaylist: false).ConfigureAwait(false);
    }

    [HttpPost("v/{token}/stop")]
    [AllowAnonymous]
    public async Task<IActionResult> Stop(string token, [FromQuery] string? ps)
    {
        var link = _store.GetValid(token);
        var sid = SafeId(ps);
        if (link is null || sid is null)
        {
            return NoContent();
        }

        if (Sessions.TryGetValue(link.Id, out var map))
        {
            map.TryRemove(sid, out _);
        }

        try
        {
            using var req = await BuildRequest(HttpMethod.Delete, link, $"/Videos/ActiveEncodings?deviceId={DeviceId(link)}&playSessionId={sid}").ConfigureAwait(false);
            using var resp = await _http.CreateClient(ShareApiKey.HttpClientName).SendAsync(req).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "[Share] stop falhou");
        }

        return NoContent();
    }

    [HttpGet("v/{token}/download/{itemId}")]
    [AllowAnonymous]
    public IActionResult Download(string token, Guid itemId)
    {
        if (!TryGetVideo(token, itemId, out var link, out var video) || !System.IO.File.Exists(video.Path))
        {
            return StatusCode(410);
        }

        _store.Touch(link);
        _logger.LogInformation("[Share] link {Id}: download de {File}", link.Id, Path.GetFileName(video.Path));
        return PhysicalFile(video.Path, "application/octet-stream", Path.GetFileName(video.Path), enableRangeProcessing: true);
    }

    /// <summary>ZIP (sem compressão, em streaming) de uma temporada, ou de tudo se groupId for o próprio item do link.</summary>
    [HttpGet("v/{token}/zip/{groupId}")]
    [AllowAnonymous]
    public async Task Zip(string token, Guid groupId)
    {
        var link = _store.GetValid(token);
        var root = link is null ? null : _library.GetItemById(link.ItemId);
        if (link is null || root is null)
        {
            Response.StatusCode = 410;
            return;
        }

        var all = Videos(link, root);
        var files = groupId == root.Id ? all : all.Where(v => v is Episode e && e.SeasonId == groupId).ToList();
        files = files.Where(v => System.IO.File.Exists(v.Path)).ToList();
        if (files.Count == 0)
        {
            Response.StatusCode = 404;
            return;
        }

        var series = SeriesOf(root);
        var zipName = groupId == root.Id
            ? link.ItemName
            : $"{series?.Name ?? root.Name} - {(files[0] as Episode)?.SeasonName ?? "Temporada"}";
        _logger.LogInformation("[Share] link {Id}: zip {Name} ({Count} arquivos)", link.Id, zipName, files.Count);
        _store.Touch(link);

        Response.ContentType = "application/zip";
        Response.Headers.ContentDisposition = "attachment; filename*=UTF-8''" + Uri.EscapeDataString(SafeFileName(zipName) + ".zip");
        var bodyControl = HttpContext.Features.Get<IHttpBodyControlFeature>();
        if (bodyControl is not null)
        {
            bodyControl.AllowSynchronousIO = true; // ZipArchive escreve parte do cabeçalho de forma síncrona
        }

        var multiSeason = files.OfType<Episode>().Select(e => e.SeasonId).Distinct().Count() > 1;
        var ct = HttpContext.RequestAborted;
        using var zip = new ZipArchive(Response.Body, ZipArchiveMode.Create, leaveOpen: true);
        var used = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var v in files)
        {
            var folder = multiSeason && v is Episode e ? SafeFileName(e.SeasonName ?? "Temporada") + "/" : string.Empty;
            var paths = new List<string> { v.Path };
            paths.AddRange(_mediaSources.GetMediaStreams(v.Id)
                .Where(s => s.IsExternal && !string.IsNullOrEmpty(s.Path) && System.IO.File.Exists(s.Path))
                .Select(s => s.Path));
            foreach (var p in paths.Distinct())
            {
                var name = folder + Path.GetFileName(p);
                if (!used.Add(name))
                {
                    continue;
                }

                var entry = zip.CreateEntry(name, CompressionLevel.NoCompression);
                entry.LastWriteTime = System.IO.File.GetLastWriteTime(p);
                await using var es = entry.Open();
                await using var fs = new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20, useAsync: true);
                await fs.CopyToAsync(es, 1 << 20, ct).ConfigureAwait(false);
            }
        }
    }

    // ------------------------------------------------------------------ helpers

    private bool CanShare() => !Config.OnlyAdmins || User.GetIsApiKey() || User.IsInRole("Administrator");

    private IActionResult Json(object o) => Content(JsonSerializer.Serialize(o, Camel), "application/json; charset=utf-8");

    private object LinkDto(ShareLink l) => new
    {
        id = l.Id,
        url = $"{PublicBase()}/Share/v/{l.Token}",
        itemId = l.ItemId.ToString("N"),
        itemType = l.ItemType,
        itemName = l.ItemName,
        createdBy = l.CreatedByName,
        createdAt = l.CreatedAt,
        expiresAt = l.ExpiresAt,
        expired = l.IsExpired,
        views = l.Views,
        lastAccess = l.LastAccess,
    };

    private string PublicBase()
    {
        if (!string.IsNullOrWhiteSpace(Config.PublicBaseUrl))
        {
            return Config.PublicBaseUrl.TrimEnd('/');
        }

        var proto = Request.Headers["X-Forwarded-Proto"].FirstOrDefault() ?? Request.Scheme;
        return $"{proto}://{Request.Host}{Request.PathBase}";
    }

    /// <summary>Registra/renova uma sessão. Com isNew, recusa se o link já tem o máximo de sessões ativas (últimos 90 s).</summary>
    private static bool TouchSession(ShareLink link, string ps, bool isNew = false)
    {
        var map = Sessions.GetOrAdd(link.Id, _ => new());
        var cutoff = DateTime.UtcNow.AddSeconds(-90);
        foreach (var kv in map)
        {
            if (kv.Value < cutoff)
            {
                map.TryRemove(kv.Key, out _);
            }
        }

        if (isNew && !map.ContainsKey(ps) && map.Count >= Math.Max(1, Config.MaxStreamsPerLink))
        {
            return false;
        }

        map[ps] = DateTime.UtcNow;
        return true;
    }

    private static string DeviceId(ShareLink l) => "share-" + l.Id;

    private static string? SafeId(string? s) =>
        !string.IsNullOrEmpty(s) && s.Length <= 64 && s.All(char.IsAsciiLetterOrDigit) ? s : null;

    private static string SafeFileName(string s)
    {
        var bad = Path.GetInvalidFileNameChars().Concat(new[] { ':', '*', '?', '"', '<', '>', '|', '\\' }).ToHashSet();
        return new string(s.Select(c => bad.Contains(c) ? '_' : c).ToArray()).Trim();
    }

    private static string Truncate(string s, int n) => s.Length <= n ? s : s[..n].TrimEnd() + "…";

    private static string EpCode(Episode e)
    {
        var code = $"T{e.ParentIndexNumber ?? 0}:E{e.IndexNumber ?? 0}";
        return e.IndexNumberEnd is int end && end != e.IndexNumber ? code + "-" + end : code;
    }

    private static string DisplayName(BaseItem item) => item switch
    {
        Season s => $"{s.SeriesName} – {s.Name}",
        Episode e => $"{e.SeriesName} – {EpCode(e)} – {e.Name}",
        Movie m when m.ProductionYear is int y => $"{m.Name} ({y})",
        Series s => $"{s.Name} (todas as temporadas)",
        _ => item.Name,
    };

    private Series? SeriesOf(BaseItem item) => item switch
    {
        Series s => s,
        Season s => _library.GetItemById(s.SeriesId) as Series,
        Episode e => _library.GetItemById(e.SeriesId) as Series,
        _ => null,
    };

    /// <summary>Todos os vídeos cobertos pelo link, em ordem (especiais no fim).</summary>
    private List<Video> Videos(ShareLink link, BaseItem root)
    {
        switch (root)
        {
            case Episode e:
                return new List<Video> { e };
            case Movie m:
                return new List<Video> { m };
            case Series or Season:
                var seriesId = root is Season se ? se.SeriesId : root.Id;
                var eps = _library.GetItemList(new InternalItemsQuery
                    {
                        AncestorIds = new[] { seriesId },
                        IncludeItemTypes = new[] { Jellyfin.Data.Enums.BaseItemKind.Episode },
                        Recursive = true,
                        IsVirtualItem = false,
                    })
                    .OfType<Episode>()
                    .Where(e => root is not Season || e.SeasonId == root.Id)
                    .Where(e => !string.IsNullOrEmpty(e.Path))
                    .OrderBy(e => e.ParentIndexNumber is null or 0 ? int.MaxValue : e.ParentIndexNumber)
                    .ThenBy(e => e.IndexNumber ?? int.MaxValue)
                    .ThenBy(e => e.SortName, StringComparer.Ordinal);
                return eps.Cast<Video>().ToList();
            default:
                return new List<Video>();
        }
    }

    private object VideoDto(Video v)
    {
        var ep = v as Episode;
        return new
        {
            id = v.Id.ToString("N"),
            name = v.Name,
            season = ep?.ParentIndexNumber,
            episode = ep?.IndexNumber,
            episodeEnd = ep?.IndexNumberEnd,
            code = ep is null ? null : EpCode(ep),
            overview = v.Overview,
            runtime = v.RunTimeTicks / TimeSpan.TicksPerSecond,
            thumbId = v.HasImage(ImageType.Primary) ? v.Id.ToString("N") : null,
            premiere = v.PremiereDate,
        };
    }

    private bool TryGetVideo(string token, Guid itemId, out ShareLink link, out Video video)
    {
        link = _store.GetValid(token)!;
        video = null!;
        if (link is null)
        {
            return false;
        }

        if (_library.GetItemById(itemId) is not Video v || string.IsNullOrEmpty(v.Path))
        {
            return false;
        }

        var ok = v.Id == link.ItemId || (v is Episode e && (e.SeriesId == link.ItemId || e.SeasonId == link.ItemId));
        if (!ok)
        {
            return false;
        }

        video = v;
        return true;
    }

    private async Task<HttpRequestMessage> BuildRequest(HttpMethod method, ShareLink link, string pathAndQuery)
    {
        var key = await _apiKey.GetAsync().ConfigureAwait(false);
        var req = new HttpRequestMessage(method, $"http://127.0.0.1:{_host.HttpPort}{Request.PathBase}{pathAndQuery}");
        req.Headers.TryAddWithoutValidation(
            "Authorization",
            $"MediaBrowser Client=\"Compartilhar\", Device=\"Link {link.Id}\", DeviceId=\"{DeviceId(link)}\", Version=\"1.0.0\", Token=\"{key}\"");
        return req;
    }

    private async Task ProxyAsync(ShareLink link, string pathAndQuery, bool isPlaylist)
    {
        var ct = HttpContext.RequestAborted;
        using var req = await BuildRequest(HttpMethod.Get, link, pathAndQuery).ConfigureAwait(false);
        using var resp = await _http.CreateClient(ShareApiKey.HttpClientName)
            .SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct).ConfigureAwait(false);
        Response.StatusCode = (int)resp.StatusCode;
        Response.Headers.CacheControl = "no-store";
        if (resp.Content.Headers.ContentType is { } type)
        {
            Response.ContentType = type.ToString();
        }

        if (!isPlaylist)
        {
            await resp.Content.CopyToAsync(Response.Body, ct).ConfigureAwait(false);
            return;
        }

        // Playlists: remove linhas que citem a chave (ex.: trickplay com ApiKey=) por segurança.
        var key = await _apiKey.GetAsync().ConfigureAwait(false);
        var body = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        var lines = body.Split('\n').Where(l =>
            !l.Contains(key, StringComparison.OrdinalIgnoreCase)
            && !l.Contains("ApiKey=", StringComparison.OrdinalIgnoreCase)
            && !l.Contains("api_key=", StringComparison.OrdinalIgnoreCase)
            && !l.StartsWith("#EXT-X-IMAGE-STREAM-INF", StringComparison.Ordinal));
        Response.ContentType = "application/vnd.apple.mpegurl";
        await Response.WriteAsync(string.Join('\n', lines), Encoding.UTF8, ct).ConfigureAwait(false);
    }

    private static string Resource(string name)
    {
        var asm = Assembly.GetExecutingAssembly();
        using var s = asm.GetManifestResourceStream("Jellyfin.Plugin.Share.Web." + name)
            ?? throw new FileNotFoundException(name);
        using var r = new StreamReader(s);
        return r.ReadToEnd();
    }
}
