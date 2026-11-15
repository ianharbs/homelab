# Títulos em português (pt-BR) nos resultados do Hardcover.
#
# O Hardcover devolve o título original (em geral inglês). Este initializer
# busca, numa única consulta extra por pesquisa, as edições em português dos
# livros encontrados e troca o título pelo da edição brasileira (ou, se não
# houver, pela edição em português mais popular). Afeta a pesquisa e os
# detalhes do livro (o título do pedido também fica em português).
#
# Montado em /rails/config/initializers/ pelo docker-compose. Se algo falhar,
# o resultado original é mantido — nunca quebra a pesquisa.
Rails.application.config.after_initialize do
  module PtBrTitles
    QUERY = <<~GRAPHQL
      query PtBrTitles($ids: [Int!]) {
        editions(where: { book_id: { _in: $ids }, language: { code2: { _eq: "pt" } } }) {
          book_id
          title
          users_count
          country { name }
        }
      }
    GRAPHQL

    NON_BR_COUNTRIES = [
      "Portugal", "Angola", "Mozambique", "Cape Verde", "Guinea-Bissau",
      "Sao Tome and Principe", "São Tomé and Príncipe", "Timor-Leste", "East Timor", "Macao", "Macau"
    ].freeze

    module_function

    # { "book_id" => "Título em português" }
    def titles_for(ids)
      ids = Array(ids).filter_map { |id| Integer(id, exception: false) }.uniq
      return {} if ids.empty?

      Rails.cache.fetch("ptbr_titles:v2:#{ids.sort.join(',')}", expires_in: 7.days) do
        response = HardcoverClient.send(:execute_query, QUERY, { ids: ids })
        Array(response.dig("data", "editions"))
          .group_by { |e| e["book_id"].to_s }
          .transform_values do |editions|
            # Edições marcadas como de Portugal/África usam títulos de Portugal
            # ("Os Jogos da Fome" em vez de "Jogos Vorazes") — ficam de fora.
            # Muitas edições brasileiras vêm sem país (ou "United States").
            editions = editions.reject { |e| NON_BR_COUNTRIES.include?(e.dig("country", "name")) }
            best = editions.max_by { |e| [e.dig("country", "name") == "Brazil" ? 1 : 0, e["users_count"].to_i] }
            best && best["title"].to_s.strip.presence
          end
          .compact
      end
    rescue StandardError => e
      Rails.logger.warn "[PtBrTitles] #{e.class}: #{e.message}"
      {}
    end

    def localize(item)
      title = titles_for([item.id])[item.id.to_s]
      title ? item.with(title: title) : item
    rescue StandardError
      item
    end
  end

  HardcoverClient.singleton_class.prepend(Module.new do
    def search(query, limit: nil)
      results = super
      begin
        titles = PtBrTitles.titles_for(results.map(&:id))
        results.map { |r| (t = titles[r.id.to_s]) ? r.with(title: t) : r }
      rescue StandardError => e
        Rails.logger.warn "[PtBrTitles] search: #{e.class}: #{e.message}"
        results
      end
    end

    def book(book_id)
      PtBrTitles.localize(super)
    end
  end)
end
