using System.Security.Cryptography;
using System.Text.Json;
using MediaBrowser.Common.Configuration;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Share;

public class ShareLink
{
    /// <summary>Id curto, público só para o admin (revogar, DeviceId no painel).</summary>
    public string Id { get; set; } = string.Empty;

    /// <summary>Segredo que vai na URL.</summary>
    public string Token { get; set; } = string.Empty;

    public Guid ItemId { get; set; }

    public string ItemType { get; set; } = string.Empty;

    public string ItemName { get; set; } = string.Empty;

    public Guid CreatedByUserId { get; set; }

    public string CreatedByName { get; set; } = string.Empty;

    public DateTime CreatedAt { get; set; }

    /// <summary>null = sem validade.</summary>
    public DateTime? ExpiresAt { get; set; }

    public int Views { get; set; }

    public DateTime? LastAccess { get; set; }

    public bool IsExpired => ExpiresAt.HasValue && ExpiresAt.Value <= DateTime.UtcNow;
}

/// <summary>Links guardados em JSON na pasta de dados do plugin (sobrevive a atualizações do plugin).</summary>
public class ShareStore
{
    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = true };
    private readonly object _lock = new();
    private readonly string _path;
    private readonly ILogger<ShareStore> _logger;
    private List<ShareLink> _links;
    private DateTime _lastViewFlush = DateTime.MinValue;

    public ShareStore(IApplicationPaths paths, ILogger<ShareStore> logger)
    {
        _logger = logger;
        var dir = Path.Combine(paths.PluginConfigurationsPath, "Jellyfin.Plugin.Share");
        Directory.CreateDirectory(dir);
        _path = Path.Combine(dir, "links.json");
        _links = Load();
    }

    private List<ShareLink> Load()
    {
        try
        {
            if (File.Exists(_path))
            {
                return JsonSerializer.Deserialize<List<ShareLink>>(File.ReadAllText(_path)) ?? new();
            }
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[Share] Falha lendo {Path}", _path);
        }

        return new();
    }

    private void Save()
    {
        var tmp = _path + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(_links, JsonOpts));
        File.Move(tmp, _path, true);
    }

    public IReadOnlyList<ShareLink> All()
    {
        lock (_lock)
        {
            return _links.OrderByDescending(l => l.CreatedAt).ToList();
        }
    }

    public ShareLink Create(Guid itemId, string itemType, string itemName, Guid userId, string userName, int days)
    {
        var link = new ShareLink
        {
            Id = Convert.ToHexString(RandomNumberGenerator.GetBytes(4)).ToLowerInvariant(),
            Token = Base64Url(RandomNumberGenerator.GetBytes(24)),
            ItemId = itemId,
            ItemType = itemType,
            ItemName = itemName,
            CreatedByUserId = userId,
            CreatedByName = userName,
            CreatedAt = DateTime.UtcNow,
            ExpiresAt = days > 0 ? DateTime.UtcNow.AddDays(days) : null,
        };
        lock (_lock)
        {
            _links.Add(link);
            Save();
        }

        return link;
    }

    /// <summary>Link válido (existe e não expirou) pelo token da URL.</summary>
    public ShareLink? GetValid(string token)
    {
        if (string.IsNullOrEmpty(token))
        {
            return null;
        }

        lock (_lock)
        {
            var l = _links.FirstOrDefault(x => CryptographicOperations.FixedTimeEquals(
                System.Text.Encoding.UTF8.GetBytes(x.Token), System.Text.Encoding.UTF8.GetBytes(token)));
            return l is null || l.IsExpired ? null : l;
        }
    }

    public void RegisterView(ShareLink link)
    {
        lock (_lock)
        {
            link.Views++;
            link.LastAccess = DateTime.UtcNow;
            Save();
        }
    }

    public void Touch(ShareLink link)
    {
        lock (_lock)
        {
            link.LastAccess = DateTime.UtcNow;
            if (DateTime.UtcNow - _lastViewFlush > TimeSpan.FromMinutes(5))
            {
                _lastViewFlush = DateTime.UtcNow;
                Save();
            }
        }
    }

    public bool Revoke(string id)
    {
        lock (_lock)
        {
            var n = _links.RemoveAll(x => x.Id == id);
            if (n > 0)
            {
                Save();
            }

            return n > 0;
        }
    }

    public int RemoveExpired()
    {
        lock (_lock)
        {
            var n = _links.RemoveAll(x => x.IsExpired);
            if (n > 0)
            {
                Save();
            }

            return n;
        }
    }

    private static string Base64Url(byte[] b) =>
        Convert.ToBase64String(b).TrimEnd('=').Replace('+', '-').Replace('/', '_');
}
